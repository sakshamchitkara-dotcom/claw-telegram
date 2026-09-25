"""Update handling: auth, commands, streaming agent replies."""

from __future__ import annotations

import asyncio
import logging
import time

from .backends.base import Backend, BackendError, Status, TextDelta, Turn
from .config import Settings
from .formatting import render, split_plain
from .ratelimit import RateLimiter
from .store import Store
from .telegram import Telegram, TelegramError

log = logging.getLogger(__name__)

COMMANDS = [
    ("start", "Introduction"),
    ("help", "What I can do"),
    ("reset", "Forget this conversation"),
    ("backend", "Show or switch the agent backend"),
    ("status", "Backend health and session info"),
    ("tasks", "Recent agent actions and approvals"),
]
LIVE_LIMIT = 3800  # chars shown while streaming; the final render splits properly


class Bot:
    def __init__(self, settings: Settings, tg: Telegram, store: Store, backends: dict[str, Backend]):
        self.s = settings
        self.tg = tg
        self.store = store
        self.backends = backends
        self.limiter = RateLimiter(settings.rate_limit_per_minute)
        self.started = time.time()
        self._busy: set[int] = set()
        self._tasks: set[asyncio.Task] = set()

    # ---- entry point ----------------------------------------------------------

    async def handle_update(self, update: dict) -> None:
        """Handle one update. Never raises; long agent turns run in the background."""
        try:
            if "message" in update:
                await self._on_message(update["message"])
        except Exception:
            log.exception("failed to handle update %s", update.get("update_id"))

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def allowed(self, user: dict | None) -> bool:
        return bool(user) and not user.get("is_bot") and user.get("id") in self.s.allowed_user_ids

    # ---- messages ---------------------------------------------------------------

    async def _on_message(self, msg: dict) -> None:
        chat_id = msg["chat"]["id"]
        user = msg.get("from")
        if not self.allowed(user):
            uid = (user or {}).get("id")
            log.warning("denied message from user %s in chat %s", uid, chat_id)
            if msg["chat"].get("type") == "private":
                await self.tg.send_message(chat_id, f"Not authorized. Your Telegram user id is {uid}; "
                                                    "the bot owner must add it to ALLOWED_USER_IDS.")
            return
        if not self.limiter.allow(user["id"]):
            await self.tg.send_message(chat_id, "Rate limit reached, please wait a minute.")
            return
        text = msg.get("text") or msg.get("caption") or ""
        if text.startswith("/"):
            cmd, _, arg = text[1:].partition(" ")
            await self._command(chat_id, cmd.split("@")[0].lower(), arg.strip())
            return
        if not text.strip():
            await self.tg.send_message(chat_id, "I can only handle text messages right now.")
            return
        await self._start_turn(chat_id, text, [])

    async def _start_turn(self, chat_id: int, text: str, images: list) -> None:
        if chat_id in self._busy:
            await self.tg.send_message(chat_id, "Still working on your previous message, one moment.")
            return
        self._busy.add(chat_id)
        name = self.backend_name(chat_id)
        turn = Turn(chat_id=chat_id, session_id=self.store.session_id(chat_id), text=text,
                    history=self.store.history(chat_id, self.s.history_limit), images=images)
        self.spawn(self._run_turn(name, turn))

    def backend_name(self, chat_id: int) -> str:
        name = self.store.backend_for(chat_id)
        return name if name in self.backends else self.s.default_backend

    async def _run_turn(self, name: str, turn: Turn) -> None:
        try:
            await self._stream_turn(name, turn)
        finally:
            self._busy.discard(turn.chat_id)

    async def _stream_turn(self, name: str, turn: Turn) -> None:
        chat_id = turn.chat_id
        backend = self.backends[name]
        await self.tg.send_chat_action(chat_id)
        placeholder = await self.tg.send_message(chat_id, "…")
        mid = placeholder["message_id"]
        text, status, last_edit, shown = "", "", 0.0, "…"
        try:
            async for ev in backend.stream(turn):
                if isinstance(ev, TextDelta):
                    text += ev.text
                    status = ""
                elif isinstance(ev, Status):
                    status = ev.text
                else:
                    await self._on_backend_event(name, turn, ev)
                    continue
                now = time.monotonic()
                if now - last_edit >= self.s.stream_edit_interval_s:
                    live = text if len(text) <= LIVE_LIMIT else "…" + text[-LIVE_LIMIT:]
                    live = (live + (f"\n\n⏳ {status}" if status else "")).strip() or "…"
                    if live != shown:
                        await self._safe_edit(chat_id, mid, live)
                        shown, last_edit = live, now
        except BackendError as e:
            log.warning("backend %s failed: %s", name, e)
            await self._safe_edit(chat_id, mid, f"⚠️ {e}")
            return
        except Exception:
            log.exception("turn failed in chat %s", chat_id)
            await self._safe_edit(chat_id, mid, "⚠️ Internal error, see bot logs.")
            return
        text = text.strip() or "(empty reply)"
        self.store.add_message(chat_id, "user", turn.text)
        self.store.add_message(chat_id, "assistant", text)
        await self._deliver(chat_id, mid, text)

    async def _on_backend_event(self, name: str, turn: Turn, ev) -> None:
        log.info("ignoring unsupported backend event %r", ev)

    async def _safe_edit(self, chat_id: int, mid: int, text: str) -> None:
        try:
            await self.tg.edit_message(chat_id, mid, text[:4096])
        except TelegramError as e:
            log.warning("edit failed: %s", e)

    async def _deliver(self, chat_id: int, mid: int, md: str) -> None:
        """Replace the placeholder with the formatted reply, splitting as needed."""
        try:
            chunks, mode = render(md), "HTML"
            await self.tg.edit_message(chat_id, mid, chunks[0], parse_mode=mode)
        except TelegramError as e:
            log.info("HTML rejected (%s), falling back to plain text", e.description)
            chunks, mode = split_plain(md), None
            await self.tg.edit_message(chat_id, mid, chunks[0])
        for chunk in chunks[1:]:
            try:
                await self.tg.send_message(chat_id, chunk, parse_mode=mode)
            except TelegramError:
                for part in split_plain(chunk):
                    await self.tg.send_message(chat_id, part)

    # ---- commands -----------------------------------------------------------------

    async def _command(self, chat_id: int, cmd: str, arg: str) -> None:
        handler = getattr(self, f"cmd_{cmd}", None)
        if handler is None:
            await self.tg.send_message(chat_id, f"Unknown command /{cmd}. Try /help.")
            return
        await handler(chat_id, arg)

    async def cmd_start(self, chat_id: int, arg: str) -> None:
        text = (f"Hi! I'm your personal assistant, backed by <b>{self.backend_name(chat_id)}</b>. "
                "Just send a message. /help lists commands.")
        await self.tg.send_message(chat_id, text, parse_mode="HTML")

    async def cmd_help(self, chat_id: int, arg: str) -> None:
        lines = [f"/{c} - {d}" for c, d in COMMANDS]
        lines += ["", "Send text, photos or documents. Side-effecting agent actions ask for approval first."]
        await self.tg.send_message(chat_id, "\n".join(lines))

    async def cmd_reset(self, chat_id: int, arg: str) -> None:
        self.store.reset(chat_id)
        await self.tg.send_message(chat_id, "Conversation cleared. New session started.")

    async def cmd_backend(self, chat_id: int, arg: str) -> None:
        current = self.backend_name(chat_id)
        if not arg:
            lines = [("• " if n == current else "  ") + n for n in self.backends]
            await self.tg.send_message(chat_id, "Backends (• = active):\n" + "\n".join(lines)
                                       + "\n\nSwitch with /backend <name>.")
            return
        if arg not in self.backends:
            await self.tg.send_message(chat_id, f"Unknown backend {arg!r}. Available: {', '.join(self.backends)}")
            return
        self.store.set_backend(chat_id, arg)
        await self.tg.send_message(chat_id, f"Switched to {arg}.")

    async def cmd_status(self, chat_id: int, arg: str) -> None:
        name = self.backend_name(chat_id)
        try:
            health = await asyncio.wait_for(self.backends[name].health(), timeout=8)
        except asyncio.TimeoutError:
            health = "timeout"
        up = int(time.time() - self.started)
        await self.tg.send_message(chat_id, "\n".join([
            f"backend: {name} ({health})",
            f"session: {self.store.session_id(chat_id)}",
            f"messages stored: {self.store.count_messages(chat_id)}",
            f"uptime: {up // 3600}h{up % 3600 // 60:02d}m",
        ]))

    async def cmd_tasks(self, chat_id: int, arg: str) -> None:
        tasks = self.store.list_tasks(chat_id)
        if not tasks:
            await self.tg.send_message(chat_id, "No agent tasks yet.")
            return
        lines = [f"#{t.id} [{t.status}] {t.backend}: {t.summary.splitlines()[0][:80]}" for t in tasks]
        await self.tg.send_message(chat_id, "\n".join(lines))
