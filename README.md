# claw-telegram

A Telegram bot that acts as your personal assistant by talking to a **self-hosted agent backend**:

- **[OpenClaw](https://docs.openclaw.ai)** gateway (formerly Clawdbot/Moltbot), including exec approvals
- **[Hermes Agent](https://hermes-agent.nousresearch.com)** (Nous Research) API server, including tool progress and approvals
- any **OpenAI-compatible** `/v1/chat/completions` endpoint, e.g. Hermes models on Ollama, vLLM, llama.cpp or OpenRouter
- **Claude** (`claude-opus-5-5` via the official `anthropic` SDK) as a fallback
- an **echo/mock** backend for tests and dry runs

Replies stream into Telegram by editing a message as tokens arrive. When the agent wants to do something
with side effects, you get an **Approve / Deny** keyboard first. If a backend goes down, the next backend in
your fallback list answers instead.

```
Telegram ──► claw-telegram ──► OpenClaw gateway  (/v1/chat/completions + WS exec approvals)
   ▲              │       └──► Hermes Agent      (/v1/chat/completions + /v1/runs/{id}/approval)
   │  keyboard    │       └──► Ollama / vLLM / OpenRouter (OpenAI-compatible)
   └──────────────┘       └──► Claude API
        failover chain + circuit breakers + health probes
        sqlite: per-chat/topic history, backend choice, session ids, users, audit log, schedules, pages
```

## Features

| | |
|---|---|
| Auth | Deny by default. Roles: **owner**, **admin**, **user**, each with its own allowed backends and approval rights. Admins manage users with `/users`. Strangers get their ID back in private chats and are ignored in groups. Callback buttons are checked the same way, and a button only works in the chat it was sent to. |
| Groups | Only in groups listed in `ALLOWED_GROUP_IDS`, and only for @mentions, replies to the bot, and commands. Forum topics are separate conversations. |
| Modes | Long polling (default) or webhook. Webhooks check Telegram's secret-token header in constant time, can be limited to an IP allowlist (with trusted-proxy `X-Forwarded-For`), and drop redelivered updates. |
| Memory | Per-chat (and per-topic) history in SQLite. Stateful backends (OpenClaw) get only the newest turn plus a stable session id. `/reset` clears history and rotates the session. `/export` and `/import` move a conversation as JSON. |
| Failover | Ordered `FALLBACK_BACKENDS`, per-backend circuit breakers, and background health probes. `/backend` shows health, circuit state and the order the next turn will try. |
| Schedules | `/remind 10m text` and `/every <cron> <prompt>` (a backend prompt run on a schedule, with the answer posted to the chat). Stored in sqlite, so they survive restarts. |
| Commands | `/start` `/help` `/reset` `/summarize` `/backend [name]` `/status` `/tasks` `/cancel` `/usage` `/remind` `/every` `/schedules` `/unschedule` `/export` `/import` `/users` `/audit` |
| Limits | Optional daily reply quota per role (`DAILY_TURNS_USER=50`); `/usage` shows where you stand. `/summarize` condenses a long chat into a summary the conversation continues from. |
| Streaming | Throttled `editMessageText` while tokens arrive, with a `⏳ tool` status line. The final reply is re-rendered as Telegram HTML from a safe Markdown subset (code fences, inline code, bold, italic, strike, headings, http(s) links). Replies longer than one message are paged in place with **Show more** buttons, without breaking code blocks. Very long ones arrive as a `.md` file. Falls back to plain text if Telegram rejects the markup. |
| Media | Photos and image documents go to backends that accept images (data-URL `image_url` parts / Claude image blocks). Small text files are inlined. Voice notes are transcribed through any OpenAI-compatible `/audio/transcriptions` endpoint. |
| Agent actions | Backend approval requests become persisted tasks with an inline keyboard. Each can be answered once, only by a role that is allowed to approve, and is **denied automatically** after `APPROVAL_TIMEOUT_S`. Every decision goes to an audit table: `/tasks` shows the chat's tasks and `/audit` shows the full log. |
| Ops | Per-user rate limit, one in-flight turn per chat (`/cancel` stops it), chats handled concurrently, `/healthz`, optional password-protected read-only `/admin` page, clean shutdown, Dockerfile, docker-compose (with optional Ollama and Hermes Agent), hardened systemd unit, CI. |

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
   ID in `OWNER_IDS` (or `ALLOWED_USER_IDS`) and restart. Until then **nobody** can use the bot.

### Users and roles

| Role | Set with | Default rights |
|---|---|---|
| owner | `OWNER_IDS` | every backend, approves actions, manages admins and users |
| admin | `ADMIN_IDS` or `/users add <id> admin` (owners only) | every backend, approves actions, manages users, `/audit` |
| user | `ALLOWED_USER_IDS` or `/users add <id>` | every backend, **can't approve** |

`ROLE_BACKENDS_USER=echo,openai` limits a role to some backends (`*` or empty = all). `ROLE_APPROVE_USER=true`
lets a role approve. If `OWNER_IDS` is empty, everyone in `ALLOWED_USER_IDS` is an owner, as in 0.1.

### Groups and topics

Add the bot to a group, send `/start@yourbot` and it replies with the group's ID. Put that ID in
`ALLOWED_GROUP_IDS`. In the group it answers `@yourbot ...`, replies to its own messages, and commands. Other
messages are ignored, and each sender still needs a role. In forum supergroups every topic has its own history
and backend. With BotFather's privacy mode on (the default) Telegram only delivers those messages anyway.

### Failover

```bash
DEFAULT_BACKEND=openclaw
FALLBACK_BACKENDS=hermes,openai   # tried in this order
HEALTH_INTERVAL_S=60              # background probes; a failed probe opens the circuit
CIRCUIT_FAILURES=3                # consecutive turn failures that open it
CIRCUIT_COOLDOWN_S=60             # then exactly one trial turn is let through
FIRST_TOKEN_TIMEOUT_S=180         # silent this long before the first event = failed
IDLE_TIMEOUT_S=300                # silent this long between events = failed
```

A backend that accepts the request and then says nothing fails after `FIRST_TOKEN_TIMEOUT_S`, so the turn
falls over instead of hanging. The clock stops while an approval prompt is open in the chat. Reasoning models
on Ollama/vLLM show `⏳ thinking…` while they think, which also counts as activity. `/cancel` stops a
reply at any time.

A turn falls back only if the backend fails **before** the user has seen output or an approval prompt, so a
half-streamed answer or a pending action is never run twice. The reply is labelled with the backend that
answered it. The label isn't stored in history. Fallbacks respect the sender's role and skip backends
that don't take images when the turn has a photo.

### Schedules

```
/remind 25m take the pizza out
/remind 07:30 standup notes
/remind 2026-10-01T09:00 renew the domain
/every 0 9 * * 1-5 Summarise my calendar for today
/every 2h Check the build status and tell me if anything is red
/schedules            /unschedule 3
```

Times use `TIMEZONE` (IANA name). Without it the host's zone is read from `/etc/localtime`, so DST changes
are followed without a restart. Durations (`/remind 3h`) are real elapsed time, a cron time in the hour
that DST skips runs just after the jump, and one in the repeated hour runs once. `/every` runs the prompt through the chat's
backend. Approval prompts from that run work as usual. Plain users can have up to `MAX_SCHEDULES_PER_USER`.

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
- **Admin page:** set `ADMIN_PASSWORD` (16+ chars) and open `http://HOST:HTTP_PORT/admin` (HTTP Basic auth,
  any user name). It is read-only: backends and circuits, running replies, pending approvals, today's usage
  and the audit log. Put it behind TLS if the port is reachable from outside.
- **Webhook:** set `BOT_MODE=webhook`, `WEBHOOK_URL=https://your.host` and a random `WEBHOOK_SECRET`
  (16-256 chars of `A-Za-z0-9_-`, e.g. `openssl rand -hex 32`), and route `https://your.host/telegram/webhook`
  to `HTTP_PORT` through a TLS proxy. Add `WEBHOOK_IP_ALLOWLIST=telegram` to accept only Telegram's published
  ranges, and `WEBHOOK_TRUSTED_PROXIES=127.0.0.1` (your proxy) so the client IP is read from
  `X-Forwarded-For`. `/healthz` is served in both modes.

## Security model

- **The bot is an operator console for your agent.** An OpenClaw gateway token or Hermes API key has
  owner-level power: shell, files, and whatever tools you enabled. Give other people the `user` role and
  restrict their backends with `ROLE_BACKENDS_USER`. Any role can still *chat* with a backend it's allowed
  to use, and a prompt alone can make an agent act on its own tools.
- Empty allowlist means nobody gets in, and no group works until it's listed. An invalid ID or CIDR stops the
  bot at startup instead of being skipped.
- Approvals: only roles with approval rights can press the buttons, only in the originating chat, only once.
  Timeouts deny. Every request and decision is written to the audit table.
- `/import` accepts only `user`/`assistant` messages (no system prompts) and is admin-only in groups.
  Scheduled prompts run with the rights of the user who created them. They stop if that user loses access.
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
| Failover Hermes Agent → Ollama (OpenAI-compatible) when the Hermes gateway stops, and back when it returns | **Confirmed live** (see observed runs) |
| Telegram forum topics: `message_thread_id` + `is_topic_message`, and the implicit reply to the topic-creation message | **From the Bot API docs**, tested against the fake Bot API only |
| Mention/reply detection in groups, `reply_parameters`, `sendDocument` multipart upload | **From the Bot API docs**, tested against the fake Bot API only |
| Telegram webhook source ranges `149.154.160.0/20`, `91.108.4.0/22` | **From Telegram's webhook guide**. They could change; the list is only used if you opt in with `telegram`. |

## Development

```bash
pip install -e '.[claude,dev]'
ruff check src tests scripts && pytest -q          # 133 tests, no network needed
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

Live failover (0.2.0): Hermes Agent API server as the primary, Ollama `hermes3:3b` over the OpenAI-compatible
API as the fallback, `HEALTH_INTERVAL_S=5`. The Hermes gateway was stopped and restarted during the run:

```
owner 4242: /backend
  bot: Backends (• = this chat):
         echo: ok [closed]
       • hermes: live; models: ok (1 models, hermes-agent present) [closed]
         openai: ok (4 models, hermes3:3b present) [closed]
       Next turn tries: hermes → openai
owner 4242: Reply with exactly one word: pong
  bot: pong
--- stopping Hermes gateway ---
owner 4242: Reply with exactly one word: pong
WARNING claw_telegram.bot: backend hermes failed: hermes: cannot reach http://127.0.0.1:8642/v1 (ClientConnectorError: ...)
  bot (HTML, 3 edits): ping
       <i>↪️ answered by openai; hermes failed</i>
owner 4242: /backend
  bot: Backends (• = this chat):
         echo: ok [closed]
       • hermes: unreachable (ClientConnectorError) [open, retry in 60s, 2 failures]
         openai: ok (4 models, hermes3:3b present) [closed]
       Next turn tries: openai
owner 4242: What is 2+2? Answer with just the number.
  bot: 4                                    (hermes skipped: circuit open)
--- restarting Hermes gateway; the next 5 s probe sees it and half-opens the circuit ---
owner 4242: Reply with exactly one word: pong
  bot: pong                                 (trial turn on hermes succeeded)
owner 4242: /backend
  bot: • hermes: live; models: ok (1 models, hermes-agent present) [closed]
       Next turn tries: hermes → openai
```

Fake Telegram, mock backend: the 0.2.0 parts of `scripts/e2e_fake_telegram.py` (the real bot process, with a dead
OpenAI-compatible backend to fail over from):

```
owner 4242: /backend openai
  bot: Switched to openai.
owner 4242: Are you there?
  bot (HTML, 3 edits): echo: Are you there?
       (history: 4 msgs)
       <i>↪️ answered by echo; openai failed</i>
owner 4242: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx... (4000 chars)
  bot (HTML, 2 edits): echo: xxxx... (3486 chars)  [buttons: 1/2 | Show more ▶]
owner 4242: [taps Show more -> pg:1:1]
  bot (edited, page 2): ...'xxxxxxxxxx\n(history: 6 msgs)'  [buttons: ◀ Prev | 2/2]
owner 4242: /remind 2s stand up
  bot: ⏰ Reminder #1 set for Fri 2026-09-25 09:31 UTC.
(waiting 3s)
  bot: ⏰ Reminder: stand up
owner 4242: /every 0 9 * * 1-5 summarise my inbox
  bot: 🔁 #2 runs `0 9 * * 1-5` on this chat's backend. Next: Mon 2026-09-28 09:00 UTC.
owner 4242 in group -100777: just chatting, not for the bot
owner 4242 in group -100777: @claw_test_bot hello from the group
  bot (HTML, 2 edits): echo: hello from the group
owner 4242: /audit
  bot: 2026-09-25 09:32:08 request by system task #1 chat 4242: echo: run shell: rm -rf ./build
       2026-09-25 09:32:10 approve by user 4242 task #1 chat 4242: run shell: rm -rf ./build
document sent: conversation-4242-20260925-093146.json (8 messages. Restore with /import.) -> 8 messages
```

Fake Telegram, 0.3.0 parts of `scripts/e2e_fake_telegram.py` (the `openai` backend here is an in-process
server that accepts the request and never answers; `FIRST_TOKEN_TIMEOUT_S=3`):

```
owner 4242: Are you there?
WARNING claw_telegram.bot: backend openai failed: openai: no response for 3s
  bot (HTML, 3 edits): echo: Are you there?
       (history: 4 msgs)
       <i>↪️ answered by echo; openai failed</i>
owner 4242: /backend
  bot: Backends (• = this chat):
         echo: ok [closed]
       • openai: ok (1 models, hung present) [closed, 1 failure]
       Next turn tries: openai → echo
owner 4242: Take your time with this one
owner 4242: /cancel
  bot (None, 1 edits): ⏹ Cancelled by 4242.
owner 4242: /summarize
  bot (HTML, 2 edits): echo: Summarize our conversation so far ... (history: 8 msgs)
       <i>🗜 This summary replaced 8 stored messages.</i>
owner 4242: /usage
  bot: Today: 6 replies (4,274 chars in, 4,407 out)
       Last 7 days: 6 replies (4,274 chars in, 4,407 out)
       Daily limit for your role (owner): none
GET /admin (no password) -> 401
GET /admin -> 200; backends: echo: ok [closed]; openai: ok (1 models, hung present) [closed, 1 failure]
```

Live Ollama `hermes3:3b` over the OpenAI-compatible API (0.3.0, fake Telegram):

```
owner 4242: My cat is called Miso. Reply with just: ok
  bot (HTML, 3 edits): ok, I see you have a lovely cat named Miso.İs
owner 4242: What is my cat called? One word.
  bot (HTML, 2 edits): ok, your cat is called Miso.
owner 4242: /summarize
  bot (HTML, 2 edits): - Cat name: Miso
       <i>🗜 This summary replaced 4 stored messages.</i>
owner 4242: /usage
  bot: Today: 3 replies (271 chars in, 89 out)
GET /admin -> 200; backends: echo: ok [closed]; openai: ok (4 models, hermes3:3b present) [closed]
```

(Answer quality in these runs comes from the small local models: `ping` for "pong" is what `hermes3:3b`
actually said. The bot passes model output through unchanged.)

## Limitations

- Plugin approvals (`plugin.approval.*`) and Hermes' `session`/`always` choices are not exposed. Approve means once.
- A backend that streams nothing the bot recognises (no content, reasoning, tool-call or vendor events) for
  `FIRST_TOKEN_TIMEOUT_S` is treated as hung, even if it is busy. Raise the timeouts for slow agents.
- `/summarize` can't compact a stateful backend's (OpenClaw's) server-side transcript; there it only shows
  the summary.
- Quotas count replies, not tokens; the character counts in `/usage` are an approximation of cost.
- If the host has no zone name (no `/etc/localtime` link or `/etc/timezone`) and `TIMEZONE` is unset, the
  bot falls back to a fixed UTC offset and logs a warning.

## License

MIT
