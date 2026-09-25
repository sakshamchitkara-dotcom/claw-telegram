# claw-telegram

A Telegram bot that acts as your personal assistant by talking to a **self-hosted agent backend**:

- **[OpenClaw](https://docs.openclaw.ai)** gateway (formerly Clawdbot/Moltbot), including exec approvals
- **[Hermes Agent](https://hermes-agent.nousresearch.com)** (Nous Research) API server, including tool progress and approvals
- any **OpenAI-compatible** `/v1/chat/completions` endpoint, e.g. Hermes models on Ollama, vLLM, llama.cpp or OpenRouter
- **Claude** (`claude-opus-5-5` via the official `anthropic` SDK) as a fallback
- an **echo/mock** backend for tests and dry runs

Replies stream into Telegram by editing a message as tokens arrive. When the agent wants to do something
with side effects, you get an **Approve / Deny** keyboard first.

```
Telegram ──► claw-telegram ──► OpenClaw gateway  (/v1/chat/completions + WS exec approvals)
   ▲              │       └──► Hermes Agent      (/v1/chat/completions + /v1/runs/{id}/approval)
   │  keyboard    │       └──► Ollama / vLLM / OpenRouter (OpenAI-compatible)
   └──────────────┘       └──► Claude API (fallback)
        sqlite: per-chat history, backend choice, session ids, approval log
```

## Features

| | |
|---|---|
| Auth | Deny-by-default allowlist of Telegram user IDs. Strangers get their ID back in private chats and are ignored in groups. Callback buttons are checked the same way, and a button only works in the chat it was sent to. |
| Modes | Long polling (default) or webhook with Telegram's secret-token header, compared in constant time. |
| Memory | Per-chat history in SQLite. Stateful backends (OpenClaw) get only the newest turn plus a stable per-chat session id. `/reset` clears history and rotates the session. |
| Commands | `/start` `/help` `/reset` `/backend [name]` `/status` `/tasks` |
| Streaming | Throttled `editMessageText` while tokens arrive, with a `⏳ tool` status line. The final reply is re-rendered as Telegram HTML from a safe Markdown subset (code fences, inline code, bold, italic, strike, headings, http(s) links). Long replies are split at 4096 chars without breaking code blocks. Falls back to plain text if Telegram rejects the markup. |
| Media | Photos and image documents go to backends that accept images (data-URL `image_url` parts / Claude image blocks). Small text files are inlined. Voice notes are transcribed through any OpenAI-compatible `/audio/transcriptions` endpoint. |
| Agent actions | Backend approval requests become persisted tasks with an inline keyboard. Each can be answered once, only by an allowlisted user, and is **denied automatically** after `APPROVAL_TIMEOUT_S`. `/tasks` shows the log. |
| Ops | Per-user rate limit, one in-flight turn per chat, `/healthz`, Dockerfile, docker-compose (with optional Ollama and Hermes Agent), hardened systemd unit, CI. |

## Quick start

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Install and configure:

   ```bash
   git clone https://github.com/sakshamchitkara-dotcom/claw-telegram && cd claw-telegram
   python -m venv .venv && . .venv/bin/activate
   pip install -e '.[claude]'
   cp .env.example .env    # set TELEGRAM_BOT_TOKEN and a backend
   ```

3. Run `set -a; . ./.env; set +a; claw-telegram`, message your bot and it will tell you your user ID. Put that
   ID in `ALLOWED_USER_IDS` and restart. Until then **nobody** can use the bot.

### Backends

| Name | Enable with | Notes |
|---|---|---|
| `openclaw` | `OPENCLAW_URL=http://127.0.0.1:18789`, `OPENCLAW_TOKEN`, optional `OPENCLAW_AGENT=openclaw/<agentId>` | Enable the endpoint in the gateway config: `gateway.http.endpoints.chatCompletions.enabled: true`. Set `OPENCLAW_APPROVALS_WS=true` to relay exec approvals to Telegram (connects as a loopback backend client, so run the bot on the gateway host). |
| `hermes` | `HERMES_URL=http://127.0.0.1:8642`, `HERMES_API_KEY` | In `~/.hermes/.env`: `API_SERVER_ENABLED=true`, `API_SERVER_KEY=...`, then `hermes gateway`. |
| `openai` | `OPENAI_BASE_URL=http://localhost:11434/v1`, `OPENAI_MODEL=hermes3` | Any OpenAI-compatible server. |
| `claude` | `ENABLE_CLAUDE=true`, `ANTHROPIC_API_KEY` | Model `claude-opus-5-5` by default (`ANTHROPIC_MODEL`), effort `medium`, plain chat. |
| `echo` | always on | `!task <cmd>` simulates an action that needs approval, `!fail` simulates an error. |

`DEFAULT_BACKEND` picks the default. Each chat can switch with `/backend <name>`.

### Deploy

- **Docker:** `docker compose up -d`. Add `--profile ollama` or `--profile hermes` to run a backend next to
  the bot on a private compose network. See the comments in `docker-compose.yml`.
- **systemd:** `deploy/claw-telegram.service` (instructions at the top of the file).
- **Webhook:** set `BOT_MODE=webhook`, `WEBHOOK_URL=https://your.host` and a random `WEBHOOK_SECRET`, and route
  `https://your.host/telegram/webhook` to `HTTP_PORT` through a TLS proxy. `/healthz` is served in both modes.

## Security model

- **The bot is an operator console for your agent.** An OpenClaw gateway token or Hermes API key has
  owner-level power: shell, files, and whatever tools you enabled. Keep `ALLOWED_USER_IDS` to yourself.
- Empty allowlist means nobody gets in. An invalid ID in the list stops the bot at startup instead of being skipped.
- Approvals: only allowlisted users can press the buttons, only in the originating chat, only once. Timeouts deny.
- For OpenClaw approvals the bot connects with scopes `operator.read`, `operator.approvals` and `operator.admin`
  and the `exec-approvals` cap (see below for why). The shared gateway token already carries that authority.
- Secrets come from the environment only. `.env` is gitignored, and the Telegram token is never logged.

## What is confirmed vs assumed

Checked against the upstream docs **and** against live local installs (OpenClaw `2026.9.6`, Hermes Agent
`main@e62a47ab`, 2026-09-25):

| Integration point | Status |
|---|---|
| OpenClaw `POST /v1/chat/completions`, Bearer gateway token, `model: openclaw/<agent>`, SSE, `data: [DONE]` | **Confirmed**: docs and live gateway |
| OpenClaw stable `user` field keeps one agent session per conversation, so only the newest message is sent | **Confirmed**: docs and live test (the agent remembered a name across turns) |
| OpenClaw `GET /v1/models` lists agent targets | **Confirmed**: docs and live |
| OpenClaw WS handshake: `connect.challenge`, then `connect` req (protocol 4), `client.id=gateway-client`, `mode=backend`, token auth without device identity on loopback | **Confirmed**: docs and live |
| OpenClaw `exec.approval.requested` event and `exec.approval.resolve {id, decision: allow-once/deny}` | **Confirmed**: docs, schema and live round trip |
| OpenClaw `exec.approval.resolved` (answered in another client or expired on the gateway) updates the Telegram prompt and disables its buttons | **Confirmed live** (a gateway-side expiry arrived as `decision: "deny"`) |
| OpenClaw clients must declare cap `exec-approvals` (and need `operator.admin` to see approvals bound to another device) | **Found in gateway source and confirmed live.** The docs don't mention it. |
| OpenClaw accepts `data:` URL images in `image_url` parts | **Assumed**. Docs cover `image_url` parts and a URL allowlist policy, but data URLs weren't tested live. |
| Routing OpenClaw approvals to a chat (the chat waiting on OpenClaw, else the lowest allowlisted user ID) | **Design choice**. The event's `sessionKey` isn't mapped back to a Telegram chat. |
| Hermes `POST /v1/chat/completions` (stateless, full history), Bearer `API_SERVER_KEY`, `model: hermes-agent`, `GET /health`, `GET /v1/models` | **Confirmed**: docs and live |
| Hermes `event: hermes.tool.progress` (`{tool, emoji, label, status}`) | **Confirmed**: source and live (shown as a `⏳` status line) |
| Hermes `event: approval.request` inside the chat-completions stream, resolved with `POST /v1/runs/{run_id}/approval {"choice": "once"/"deny"}` | **Confirmed**: source and live (Approve ran `rm -rf` on a test dir; timeout-deny made the agent report the command as blocked) |
| Hermes `X-Hermes-Session-Key` for a stable per-chat memory scope | **Confirmed in docs**. Sent, but its memory effect wasn't observed. |
| OpenAI-compatible adapter against Ollama | **Confirmed live** with `hermes3:3b` (streaming, code blocks, multi-turn memory) |
| Claude adapter request shape (`claude-opus-5-5`, `output_config.effort`, no `thinking`/sampling params, refusal handling) | **Checked against the SDK and a fake Messages API only**. No API key was available for a live call. |
| Voice transcription via `/audio/transcriptions` | **Tested against a fake endpoint only** |

## Development

```bash
pip install -e '.[claude,dev]'
ruff check src tests scripts && pytest -q          # 65 tests, no network needed
python scripts/e2e_fake_telegram.py               # real bot process <-> fake Telegram <-> mock backend
python scripts/e2e_fake_telegram.py --backend openai \
  --env OPENAI_BASE_URL=http://localhost:11434/v1 --env OPENAI_MODEL=hermes3:3b
```

`tests/fake_telegram.py` is a small in-memory Bot API. It rejects over-long messages and HTML with
unsupported or unbalanced tags the way Telegram does, and can be run on its own (`python tests/fake_telegram.py`)
with `TELEGRAM_API_BASE` pointed at it.

### Observed runs (excerpts)

Mock backend, approval flow (`scripts/e2e_fake_telegram.py`):

```
owner 4242: !task rm -rf ./build
  bot (None, 1 edits): ⏳ planning: rm -rf ./build
  bot: 🔐 Approval needed (task #1, echo):
       run shell: rm -rf ./build  [buttons: ✅ Approve | ❌ Deny]
owner 4242: [taps Approve -> ap:1:1]
  bot (edited): Executed <code>rm -rf ./build</code> (mock).
owner 4242: /tasks
  bot: #1 [approved] echo: run shell: rm -rf ./build
GET /healthz -> 200 {"ok": true, "mode": "polling", "backends": ["echo"], "uptime_s": 12, "updates": 7, ...}
```

Live OpenClaw 2026.9.6 gateway (Ollama `hermes3:3b` as its model): The name in the transcript is substituted; the rest is verbatim.

```
owner 4242: /status
  bot: backend: openclaw (ok (3 models, openclaw/default present); approvals ws connected)
owner 4242: My name is Alex. Reply with just: hello <my name>
  bot (HTML, 2 edits): hello Alex
owner 4242: What is my name? One word.
  bot (HTML, 2 edits): Alex
--- exec approval raised on the gateway (openclaw gateway call exec.approval.request) ---
telegram prompt: '🔐 Approval needed (task #1, openclaw):\n\nexec on gateway: rm -rf /tmp/claw-demo' ['✅ Approve', '❌ Deny']
exec.approval.request returned: {"id": "b1294d24-...", "decision": "allow-once", ...}
```

Live Hermes Agent API server (Ollama `qwen3:4b` as its model):

```
owner 4242: Call the terminal tool with command 'rm -rf /tmp/claude-501/hh-demo'. ...
  bot (None, 1 edits): ⏳ 💻 rm -rf /tmp/claude-501/hh-demo
  bot: 🔐 Approval needed (task #1, hermes):
       delete in root path
       rm -rf /tmp/claude-501/hh-demo  [buttons: ✅ Approve | ❌ Deny]
owner 4242: [taps Approve -> ap:1:1]
$ ls /tmp/claude-501/hh-demo
ls: /tmp/claude-501/hh-demo: No such file or directory
```

(Answer quality in these runs comes from the small local models. The bot passes their output through unchanged.)

## Limitations

- Plugin approvals (`plugin.approval.*`) and Hermes' `session`/`always` choices are not exposed. Approve means once.
- Updates are handled in order. A long voice transcription delays other chats' updates in polling mode.

## License

MIT
