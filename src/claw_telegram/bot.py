"""Update handling: auth, commands, streaming agent replies."""

from __future__ import annotations

import asyncio
import logging
import os
import time

from .backends.base import ApprovalRequest, Backend, BackendError, Image, Status, TextDelta, Turn
from .config import Settings
from .formatting import render, split_plain
from .ratelimit import RateLimiter
from .store import Store
from .telegram import Telegram, TelegramError
from .transcribe import Transcriber, TranscriptionError

log = logging.getLogger(__name__)

COMMANDS = [
    ("start", "Introduction"),
    ("help", "What I can do"),
    ("reset", "Forget this conversation"),
    ("backend", "Show or switch the agent backend"),
    ("status", "Backend health and session info"),
    ("tasks", "Recent agent actions and approvals"),
]
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TEXT_DOC_BYTES = 200 * 1024
MAX_AUDIO_BYTES = 20 * 1024 * 1024
TEXT_EXTENSIONS = {".txt", ".md", ".py", ".js", ".ts", ".json", ".csv", ".log", ".yaml", ".yml", ".toml",
                   ".ini", ".cfg", ".html", ".xml", ".sh", ".sql", ".rs", ".go", ".java", ".c", ".h", ".cpp"}
LIVE_LIMIT = 3800  # chars shown while streaming; the final render splits properly


def is_text_document(name: str, mime: str | None) -> bool:
    mime = mime or ""
    if mime.startswith("text/") or mime in {"application/json", "application/xml", "application/x-yaml"}:
        return True
    return os.path.splitext(name.lower())[1] in TEXT_EXTENSIONS


class Bot:
    def __init__(self, settings: Settings, tg: Telegram, store: Store, backends: dict[str, Backend],
                 transcriber: Transcriber | None = None):
        self.s = settings
        self.transcriber = transcriber
        self.tg = tg
        self.store = store
        self.backends = backends
        self.limiter = RateLimiter(settings.rate_limit_per_minute)
        self.started = time.time()
        self._busy: dict[int, str] = {}  # chat_id -> backend name of the in-flight turn
        self._tasks: set[asyncio.Task] = set()
        self._timers: dict[int, asyncio.Task] = {}  # approval task id -> expiry timer
        for name, backend in backends.items():
            if hasattr(backend, "approval_sink"):  # backends that raise approvals out of band
                backend.approval_sink = lambda req, name=name: self._external_approval(name, req)

    # ---- entry point ----------------------------------------------------------

    async def handle_update(self, update: dict) -> None:
        """Handle one update. Never raises; long agent turns run in the background."""
        try:
            if "message" in update:
                await self._on_message(update["message"])
            elif "callback_query" in update:
                await self._on_callback(update["callback_query"])
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
        try:
            text, images, problem = await self._attachments(chat_id, msg, text)
        except TelegramError as e:
            text, images, problem = "", [], f"Could not download the attachment ({e.description})."
        if problem:
            await self.tg.send_message(chat_id, problem)
            return
        if not text.strip() and not images:
            await self.tg.send_message(chat_id, "Send me text, a photo, a document or a voice note.")
            return
        await self._start_turn(chat_id, text, images)

    async def _attachments(self, chat_id: int, msg: dict, text: str) -> tuple[str, list[Image], str | None]:
        """Pull photos/documents into the turn. Returns (text, images, problem-to-report)."""
        backend = self.backends[self.backend_name(chat_id)]
        images: list[Image] = []
        doc = msg.get("document")
        voice = msg.get("voice") or msg.get("audio")
        if voice:
            if self.transcriber is None:
                return text, [], "Voice notes need a transcription endpoint (set TRANSCRIBE_URL)."
            audio = await self.tg.download_file(voice["file_id"], MAX_AUDIO_BYTES)
            await self.tg.send_chat_action(chat_id, "typing")
            try:
                heard = await self.transcriber(audio, voice.get("file_name") or "voice.ogg")
            except TranscriptionError as e:
                return text, [], f"Transcription failed: {e}"
            if not heard:
                return text, [], "I couldn't hear anything in that voice note."
            await self.tg.send_message(chat_id, f"🎙 {heard[:4000]}")
            text = f"{text}\n\n{heard}".strip()
        elif msg.get("photo") or (doc and (doc.get("mime_type") or "").startswith("image/")):
            if not backend.supports_images:
                return text, [], f"The {backend.name} backend does not accept images."
            if msg.get("photo"):
                file_id, mime = msg["photo"][-1]["file_id"], "image/jpeg"  # last = largest size
            else:
                file_id, mime = doc["file_id"], doc["mime_type"]
            images.append(Image(mime, await self.tg.download_file(file_id, MAX_IMAGE_BYTES)))
        elif doc:
            name = doc.get("file_name") or "document"
            if not is_text_document(name, doc.get("mime_type")):
                return text, [], f"Can't read {name}: only images and text files are supported."
            if doc.get("file_size", 0) > MAX_TEXT_DOC_BYTES:
                return text, [], f"{name} is too large (max {MAX_TEXT_DOC_BYTES // 1024} KB for text files)."
            body = (await self.tg.download_file(doc["file_id"], MAX_TEXT_DOC_BYTES)).decode("utf-8", "replace")
            text = f"{text}\n\nAttached file {name}:\n```\n{body}\n```".strip()
        return text, images, None

    async def _start_turn(self, chat_id: int, text: str, images: list) -> None:
        if chat_id in self._busy:
            await self.tg.send_message(chat_id, "Still working on your previous message, one moment.")
            return
        name = self.backend_name(chat_id)
        self._busy[chat_id] = name
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
            self._busy.pop(turn.chat_id, None)

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
        if isinstance(ev, ApprovalRequest):
            await self.ask_approval(turn.chat_id, name, ev)
        else:
            log.info("ignoring unsupported backend event %r", ev)

    # ---- approvals ------------------------------------------------------------------

    async def _external_approval(self, name: str, req: ApprovalRequest) -> None:
        """Approval raised outside a reply stream (OpenClaw exec approvals).

        Routed to the chat currently waiting on that backend, else to the first
        allowlisted user's private chat.
        """
        chats = [c for c, n in self._busy.items() if n == name]
        if chats:
            chat_id = chats[-1]
        elif self.s.allowed_user_ids:
            chat_id = min(self.s.allowed_user_ids)
        else:
            log.warning("approval %s from %s dropped: nobody is allowlisted", req.ref, name)
            return
        await self.ask_approval(chat_id, name, req)

    async def ask_approval(self, chat_id: int, name: str, req: ApprovalRequest) -> None:
        tid = self.store.create_task(chat_id, name, req.ref, req.summary)
        text = f"🔐 Approval needed (task #{tid}, {name}):\n\n{req.summary[:3500]}"
        keyboard = {"inline_keyboard": [[{"text": "✅ Approve", "callback_data": f"ap:{tid}:1"},
                                         {"text": "❌ Deny", "callback_data": f"ap:{tid}:0"}]]}
        msg = await self.tg.send_message(chat_id, text, reply_markup=keyboard)
        self._timers[tid] = asyncio.create_task(self._expire(tid, chat_id, msg["message_id"], text))

    async def _expire(self, tid: int, chat_id: int, mid: int, text: str) -> None:
        await asyncio.sleep(self.s.approval_timeout_s)
        self._timers.pop(tid, None)
        task = self.store.get_task(tid)
        if task and self.store.resolve_task(tid, "expired"):
            try:
                await self.backends[task.backend].resolve_approval(task.ref, False)
            except BackendError as e:
                log.warning("could not deny expired approval %s: %s", tid, e)
            await self._safe_edit(chat_id, mid, f"{text}\n\n⌛ No answer in {self.s.approval_timeout_s}s: denied.")

    async def _on_callback(self, cq: dict) -> None:
        user = cq.get("from")
        msg = cq.get("message") or {}
        if not self.allowed(user):
            log.warning("denied callback from user %s", (user or {}).get("id"))
            await self.tg.answer_callback(cq["id"], "Not authorized.")
            return
        kind, _, rest = (cq.get("data") or "").partition(":")
        tid_s, _, choice = rest.partition(":")
        if kind != "ap" or not tid_s.isdigit() or choice not in {"0", "1"}:
            await self.tg.answer_callback(cq["id"], "Unknown action.")
            return
        task = self.store.get_task(int(tid_s))
        if task is None or task.chat_id != (msg.get("chat") or {}).get("id"):
            await self.tg.answer_callback(cq["id"], "Unknown task.")
            return
        approve = choice == "1"
        if not self.store.resolve_task(task.id, "approved" if approve else "denied"):
            await self.tg.answer_callback(cq["id"], "Already resolved.")
            return
        if timer := self._timers.pop(task.id, None):
            timer.cancel()
        who = user.get("username") or user.get("first_name") or str(user["id"])
        outcome = f"✅ Approved by {who}" if approve else f"❌ Denied by {who}"
        try:
            await self.backends[task.backend].resolve_approval(task.ref, approve)
        except (BackendError, KeyError) as e:
            self.store.set_task_status(task.id, "error")
            outcome += f", but the backend did not accept it: {e}"
        await self.tg.answer_callback(cq["id"], outcome[:190])
        await self._safe_edit(task.chat_id, msg["message_id"], f"{msg.get('text', '')}\n\n{outcome}")

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
