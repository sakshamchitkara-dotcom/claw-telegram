# Changelog

All notable changes to this project. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/).

## [0.3.0] - 2026-09-25

### Added

- `FIRST_TOKEN_TIMEOUT_S` (default 180) and `IDLE_TIMEOUT_S` (default 300): a backend that accepts a request
  and then goes silent fails, so the turn falls over instead of waiting for the 600 s HTTP timeout. The clock
  stops while an approval prompt is open in the chat.
- `/cancel` stops the reply running in the chat and denies the approval it was waiting on, if any.
- `/summarize` asks the backend for a summary and replaces the stored history with it (stateless backends).
- Daily reply quotas per role (`DAILY_TURNS_<ROLE>`) and `/usage` (`/usage all` for admins), backed by a new
  `usage` table (schema migration 2).
- Read-only admin page at `/admin` behind `ADMIN_PASSWORD` (HTTP Basic auth): backends and circuits, running
  replies, pending approvals, today's usage and the audit log.
- OpenAI-compatible reasoning models (`delta.reasoning` / `reasoning_content`) show `⏳ thinking…`.

### Fixed

- A half-open circuit lets exactly one trial turn through; concurrent turns use their fallbacks.
- A turn waiting on an out-of-band (OpenClaw) approval is never retried on a fallback backend.
- Without `TIMEZONE` the host's IANA zone is used, so DST changes no longer need a restart. `/remind 3h`
  and `/every 3h` are real elapsed time across DST, and a cron time in the repeated hour runs once.
- `TZ=:/etc/localtime` and other libc-style `TZ` values no longer crash startup; a bad `TIMEZONE` stops the
  bot with a clear message.
- Chats are handled concurrently in polling mode (a slow voice note no longer blocks other chats), and each
  chat's updates stay in order in both modes.
- Shutdown marks interrupted replies and denies open approvals instead of leaving "…" and live buttons.
  Approvals left pending by a crash are denied at the next start.

## [0.2.0] - 2026-09-25

### Added

- **Roles.** `OWNER_IDS`, `ADMIN_IDS` and `ALLOWED_USER_IDS` map to owner, admin and user. `ROLE_BACKENDS_<ROLE>`
  limits the backends each role can see and use. `ROLE_APPROVE_<ROLE>` controls who can press Approve/Deny
  (owners and admins by default, users not).
- `/users` (admins): list users, `add <id> [user|admin]`, `remove <id>`. Admins manage users; only owners can
  add or remove admins. Users set in the environment can't be changed from chat.
- **Audit log** in sqlite: approval requests, approvals, denials, expiries, approvals answered outside
  Telegram, and user changes. `/audit [n]` (admins) shows it.
- **Scheduling.** `/remind <10m|2h30m|14:30|2026-10-01T09:00> <text>` and
  `/every <cron|@daily|2h> <prompt>`. `/every` runs the prompt through the chat's backend and streams the
  result into the chat. `/schedules` lists them and `/unschedule <id>` cancels one. Schedules are stored in
  sqlite and survive restarts; runs missed while the bot was down fire once, late. Cron is parsed in-house
  (lists, ranges, steps, names, @aliases), so there's no new dependency. Settings: `TIMEZONE`,
  `MAX_SCHEDULES_PER_USER`.
- **Group chats.** Groups have to be listed in `ALLOWED_GROUP_IDS`. In a group the bot only answers
  @mentions (stripped from the prompt), replies to its own messages, and commands. `/cmd@otherbot` is
  ignored. Command answers in groups are sent as replies to the asker.
- **Forum topics.** Each topic has its own history, backend, session and schedules. Replies and approval
  prompts go back into the same topic.
- **Backend failover.** `FALLBACK_BACKENDS` is an ordered list to try when the chat's backend fails before it
  has shown any output. Each backend has a circuit breaker (`CIRCUIT_FAILURES`, `CIRCUIT_COOLDOWN_S`) and
  gets background health probes (`HEALTH_INTERVAL_S`). A fallback reply is labelled with the backend that
  answered it.
- `/backend` probes every backend and shows its health, circuit state and the order the next turn will try.
- **Long replies.** Replies that need more than one message stay a single message with
  `◀ Prev | n/N | Show more ▶` buttons; the pages are kept in sqlite for 7 days. Replies over
  `LONG_REPLY_FILE_CHARS` (default 12000) show a preview in the chat and arrive in full as a `.md` file.
- **Webhook hardening.** `WEBHOOK_IP_ALLOWLIST` accepts CIDRs, or `telegram` for Telegram's published
  ranges. `WEBHOOK_TRUSTED_PROXIES` controls which peers' `X-Forwarded-For` is trusted. Redelivered
  `update_id`s are dropped, and bodies that aren't an update get a 400. `/healthz` reports `duplicates`.
- `/export` sends the conversation as JSON. `/import` (the file's caption, or a reply to the file) restores
  it after validation and starts a fresh backend session.
- The fake-Telegram end-to-end script covers all of the above and exits non-zero if an expected reply is
  missing.

### Changed

- The sqlite schema is versioned with `PRAGMA user_version` and migrates automatically. 0.1 databases are
  upgraded in place and existing OpenClaw session ids are kept.
- Out-of-band approvals (OpenClaw) with no waiting chat now go to the lowest owner ID.

### Breaking

- **Groups are off until you list them** in `ALLOWED_GROUP_IDS`. In 0.1 an allowlisted user could use the
  bot in any group, and it answered every message there.
- **`WEBHOOK_SECRET` must be 16-256 characters** from `A-Z a-z 0-9 _ -`. The bot refuses to start otherwise.
- With `OWNER_IDS` set, people listed only in `ALLOWED_USER_IDS` become plain users and **can't approve
  actions** by default. Leave `OWNER_IDS` empty to keep the 0.1 behaviour, where everyone in
  `ALLOWED_USER_IDS` is an owner.

## [0.1.0] - 2026-09-25

First release: a Telegram front-end for OpenClaw, Hermes Agent, OpenAI-compatible endpoints and Claude.
Streaming replies, Approve/Deny for agent actions (OpenClaw exec approvals over the gateway WebSocket, Hermes
run approvals), photos/documents/voice notes, per-chat sqlite memory, allowlist auth, rate limiting, polling
and webhook modes, Docker and systemd deployment.

[0.3.0]: https://github.com/sakshamchitkara-dotcom/claw-telegram/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/sakshamchitkara-dotcom/claw-telegram/compare/52eef20...v0.2.0
[0.1.0]: https://github.com/sakshamchitkara-dotcom/claw-telegram/tree/52eef20
