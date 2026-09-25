"""Update handling: auth, commands, streaming agent replies."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .backends.base import ApprovalRequest, Backend, BackendError, Image, Status, TextDelta, Turn
from .config import ROLES, Settings
from .formatting import render, split_plain
from .ratelimit import RateLimiter
from .schedule import next_run, parse_when, split_spec
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
    ("remind", "Remind me: /remind 10m text, /remind 14:30 text"),
    ("every", "Repeat a prompt: /every 0 9 * * * prompt, /every 2h prompt"),
    ("schedules", "List reminders and repeating prompts"),
    ("unschedule", "Cancel a reminder or repeating prompt"),
    ("users", "Admins: list, add or remove users"),
    ("audit", "Admins: approval and user-change log"),
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


@dataclass
class Ctx:
    """Who sent a message, where, and with which role."""
    chat_id: int
    user_id: int
    role: str
    msg: dict


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
        # ponytail: without TIMEZONE the host's current UTC offset is used, so DST changes need a restart.
        self.tz = ZoneInfo(settings.timezone) if settings.timezone else datetime.now().astimezone().tzinfo
        self._sched_wake = asyncio.Event()
        self._busy: dict[int, str] = {}  # chat_id -> backend name of the in-flight turn
        self._tasks: set[asyncio.Task] = set()
        self._timers: dict[int, asyncio.Task] = {}  # approval task id -> expiry timer
        self._prompts: dict[int, tuple[int, int, str]] = {}  # task id -> (chat, message id, text)
        for name, backend in backends.items():
            if hasattr(backend, "approval_sink"):  # backends that raise approvals out of band
                backend.approval_sink = lambda req, name=name: self._external_approval(name, req)
            if hasattr(backend, "resolved_sink"):  # ...and can report approvals answered elsewhere
                backend.resolved_sink = lambda ref, decision, name=name: self._resolved_elsewhere(name, ref, decision)

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

    def role_of(self, user: dict | None) -> str | None:
        """owner/admin/user, or None for strangers. Env roles win over runtime-added ones."""
        if not user or user.get("is_bot"):
            return None
        return self.s.env_role(user["id"]) or self.store.user_role(user["id"])

    def allowed(self, user: dict | None) -> bool:
        return self.role_of(user) is not None

    # ---- messages ---------------------------------------------------------------

    async def _on_message(self, msg: dict) -> None:
        chat_id = msg["chat"]["id"]
        user = msg.get("from")
        role = self.role_of(user)
        if role is None:
            uid = (user or {}).get("id")
            log.warning("denied message from user %s in chat %s", uid, chat_id)
            if msg["chat"].get("type") == "private":
                await self.tg.send_message(chat_id, f"Not authorized. Your Telegram user id is {uid}; "
                                                    "the bot owner must add it to ALLOWED_USER_IDS.")
            return
        if not self.limiter.allow(user["id"]):
            await self.tg.send_message(chat_id, "Rate limit reached, please wait a minute.")
            return
        ctx = Ctx(chat_id, user["id"], role, msg)
        text = msg.get("text") or msg.get("caption") or ""
        if text.startswith("/"):
            cmd, _, arg = text[1:].partition(" ")
            await self._command(ctx, cmd.split("@")[0].lower(), arg.strip())
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
        await self._start_turn(ctx, text, images)

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

    async def _start_turn(self, ctx: Ctx, text: str, images: list) -> None:
        chat_id = ctx.chat_id
        if chat_id in self._busy:
            await self.tg.send_message(chat_id, "Still working on your previous message, one moment.")
            return
        name = self.backend_name(chat_id)
        if not self.s.backend_allowed(ctx.role, name):
            await self.tg.send_message(chat_id, f"Your role ({ctx.role}) can't use the {name} backend. "
                                                "Pick another with /backend.")
            return
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
        elif self.s.owners:
            chat_id = min(self.s.owners)
        else:
            log.warning("approval %s from %s dropped: nobody is allowlisted", req.ref, name)
            return
        await self.ask_approval(chat_id, name, req)

    async def ask_approval(self, chat_id: int, name: str, req: ApprovalRequest) -> None:
        tid = self.store.create_task(chat_id, name, req.ref, req.summary)
        self.store.audit("request", chat_id=chat_id, task_id=tid, detail=f"{name}: {req.summary[:500]}")
        text = f"🔐 Approval needed (task #{tid}, {name}):\n\n{req.summary[:3500]}"
        keyboard = {"inline_keyboard": [[{"text": "✅ Approve", "callback_data": f"ap:{tid}:1"},
                                         {"text": "❌ Deny", "callback_data": f"ap:{tid}:0"}]]}
        msg = await self.tg.send_message(chat_id, text, reply_markup=keyboard)
        self._prompts[tid] = (chat_id, msg["message_id"], text)
        self._timers[tid] = asyncio.create_task(self._expire(tid, chat_id, msg["message_id"], text))

    async def _resolved_elsewhere(self, name: str, ref: str, decision: str) -> None:
        """The backend reports an approval was answered by another client (e.g. OpenClaw Control UI)."""
        task = self.store.find_pending_task(name, ref)
        if task is None:
            return  # unknown, or we resolved it ourselves
        approved = decision.startswith("allow")
        if not self.store.resolve_task(task.id, "approved" if approved else "denied"):
            return
        self.store.audit("approve" if approved else "deny", chat_id=task.chat_id, task_id=task.id,
                         detail=f"{decision} outside Telegram")
        if timer := self._timers.pop(task.id, None):
            timer.cancel()
        if prompt := self._prompts.pop(task.id, None):
            chat_id, mid, text = prompt
            mark = "✅" if approved else "❌"
            await self._safe_edit(chat_id, mid, f"{text}\n\n{mark} {decision} (resolved outside Telegram)")

    async def _expire(self, tid: int, chat_id: int, mid: int, text: str) -> None:
        await asyncio.sleep(self.s.approval_timeout_s)
        self._timers.pop(tid, None)
        self._prompts.pop(tid, None)
        task = self.store.get_task(tid)
        if task and self.store.resolve_task(tid, "expired"):
            self.store.audit("expire", chat_id=chat_id, task_id=tid,
                             detail=f"no answer in {self.s.approval_timeout_s}s")
            try:
                await self.backends[task.backend].resolve_approval(task.ref, False)
            except BackendError as e:
                log.warning("could not deny expired approval %s: %s", tid, e)
            await self._safe_edit(chat_id, mid, f"{text}\n\n⌛ No answer in {self.s.approval_timeout_s}s: denied.")

    async def _on_callback(self, cq: dict) -> None:
        user = cq.get("from")
        msg = cq.get("message") or {}
        role = self.role_of(user)
        if role is None:
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
        if not self.s.can_approve(role):
            await self.tg.answer_callback(cq["id"], f"Your role ({role}) can't answer approvals.")
            return
        approve = choice == "1"
        if not self.store.resolve_task(task.id, "approved" if approve else "denied"):
            await self.tg.answer_callback(cq["id"], "Already resolved.")
            return
        if timer := self._timers.pop(task.id, None):
            timer.cancel()
        self._prompts.pop(task.id, None)
        who = user.get("username") or user.get("first_name") or str(user["id"])
        outcome = f"✅ Approved by {who}" if approve else f"❌ Denied by {who}"
        detail = task.summary[:500]
        try:
            await self.backends[task.backend].resolve_approval(task.ref, approve)
        except (BackendError, KeyError) as e:
            self.store.set_task_status(task.id, "error")
            outcome += f", but the backend did not accept it: {e}"
            detail += f" [backend error: {e}]"
        self.store.audit("approve" if approve else "deny", user_id=user["id"], chat_id=task.chat_id,
                         task_id=task.id, detail=detail)
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

    async def _command(self, ctx: Ctx, cmd: str, arg: str) -> None:
        handler = getattr(self, f"cmd_{cmd}", None)
        if handler is None:
            await self.tg.send_message(ctx.chat_id, f"Unknown command /{cmd}. Try /help.")
            return
        await handler(ctx, arg)

    async def cmd_start(self, ctx: Ctx, arg: str) -> None:
        chat_id = ctx.chat_id
        text = (f"Hi! I'm your personal assistant, backed by <b>{self.backend_name(chat_id)}</b>. "
                "Just send a message. /help lists commands.")
        await self.tg.send_message(chat_id, text, parse_mode="HTML")

    async def cmd_help(self, ctx: Ctx, arg: str) -> None:
        chat_id = ctx.chat_id
        lines = [f"/{c} - {d}" for c, d in COMMANDS]
        lines += ["", "Send text, photos or documents. Side-effecting agent actions ask for approval first."]
        await self.tg.send_message(chat_id, "\n".join(lines))

    async def cmd_reset(self, ctx: Ctx, arg: str) -> None:
        chat_id = ctx.chat_id
        self.store.reset(chat_id)
        await self.tg.send_message(chat_id, "Conversation cleared. New session started.")

    async def cmd_backend(self, ctx: Ctx, arg: str) -> None:
        chat_id = ctx.chat_id
        current = self.backend_name(chat_id)
        usable = [n for n in self.backends if self.s.backend_allowed(ctx.role, n)]
        if not arg:
            lines = [("• " if n == current else "  ") + n for n in usable]
            await self.tg.send_message(chat_id, "Backends (• = active):\n" + "\n".join(lines)
                                       + "\n\nSwitch with /backend <name>.")
            return
        if arg not in usable:
            await self.tg.send_message(chat_id, f"Unknown backend {arg!r}. Available: {', '.join(usable)}")
            return
        self.store.set_backend(chat_id, arg)
        await self.tg.send_message(chat_id, f"Switched to {arg}.")

    async def cmd_status(self, ctx: Ctx, arg: str) -> None:
        chat_id = ctx.chat_id
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

    async def cmd_tasks(self, ctx: Ctx, arg: str) -> None:
        chat_id = ctx.chat_id
        tasks = self.store.list_tasks(chat_id)
        if not tasks:
            await self.tg.send_message(chat_id, "No agent tasks yet.")
            return
        lines = [f"#{t.id} [{t.status}] {t.backend}: {t.summary.splitlines()[0][:80]}" for t in tasks]
        await self.tg.send_message(chat_id, "\n".join(lines))

    def _at_least(self, ctx: Ctx, role: str) -> bool:
        return ROLES.index(ctx.role) <= ROLES.index(role)

    async def cmd_users(self, ctx: Ctx, arg: str) -> None:
        chat_id = ctx.chat_id
        if not self._at_least(ctx, "admin"):
            await self.tg.send_message(chat_id, "Only admins can manage users.")
            return
        action, *rest = arg.split() or ["list"]
        if action == "list":
            s = self.s
            env = [f"{r}: {', '.join(map(str, sorted(ids)))}" for r, ids in
                   (("owner", s.owners), ("admin", s.admin_ids),
                    ("user", s.allowed_user_ids if s.owner_ids else frozenset())) if ids]
            added = [f"{uid} ({role}, added by {by})" for uid, role, by in self.store.list_users()]
            text = "From environment:\n" + ("\n".join(env) or "(none)")
            text += "\n\nAdded with /users:\n" + ("\n".join(added) or "(none)")
            text += "\n\n/users add <id> [user|admin] · /users remove <id>"
            await self.tg.send_message(chat_id, text)
            return
        if action not in {"add", "remove"} or not rest or not rest[0].lstrip("-").isdigit():
            await self.tg.send_message(chat_id, "Usage: /users [list] | add <id> [user|admin] | remove <id>")
            return
        uid = int(rest[0])
        if self.s.env_role(uid):
            await self.tg.send_message(chat_id, f"{uid} is set in the environment ({self.s.env_role(uid)}); "
                                                "change it there.")
            return
        if action == "add":
            role = rest[1] if len(rest) > 1 else "user"
            if role not in {"user", "admin"}:
                await self.tg.send_message(chat_id, "Role must be user or admin.")
                return
            if role == "admin" and ctx.role != "owner":
                await self.tg.send_message(chat_id, "Only owners can add admins.")
                return
            if self.store.user_role(uid) == "admin" and ctx.role != "owner":
                await self.tg.send_message(chat_id, "Only owners can change an admin.")
                return
            self.store.set_user(uid, role, ctx.user_id)
            self.store.audit("user.add", user_id=ctx.user_id, chat_id=chat_id, detail=f"{uid} as {role}")
            await self.tg.send_message(chat_id, f"Added {uid} as {role}.")
            return
        current = self.store.user_role(uid)
        if current is None:
            await self.tg.send_message(chat_id, f"{uid} is not a runtime user.")
            return
        if current == "admin" and ctx.role != "owner":
            await self.tg.send_message(chat_id, "Only owners can remove admins.")
            return
        self.store.remove_user(uid)
        self.store.audit("user.remove", user_id=ctx.user_id, chat_id=chat_id, detail=f"{uid} ({current})")
        await self.tg.send_message(chat_id, f"Removed {uid}.")

    async def cmd_audit(self, ctx: Ctx, arg: str) -> None:
        if not self._at_least(ctx, "admin"):
            await self.tg.send_message(ctx.chat_id, "Only admins can read the audit log.")
            return
        limit = min(int(arg), 100) if arg.isdigit() and int(arg) > 0 else 20
        entries = self.store.audit_log(limit)
        if not entries:
            await self.tg.send_message(ctx.chat_id, "Audit log is empty.")
            return
        lines = []
        for e in reversed(entries):
            who = f"user {e.user_id}" if e.user_id is not None else "system"
            where = f" task #{e.task_id}" if e.task_id else ""
            where += f" chat {e.chat_id}" if e.chat_id is not None else ""
            first = e.detail.splitlines()[0][:120] if e.detail else ""
            lines.append(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(e.ts))} {e.action} by {who}{where}"
                         + (f": {first}" if first else ""))
        for chunk in split_plain("\n".join(lines)):
            await self.tg.send_message(ctx.chat_id, chunk)

    # ---- schedules --------------------------------------------------------------

    def _fmt(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, self.tz).strftime("%a %Y-%m-%d %H:%M %Z").strip()

    async def _schedule_quota_ok(self, ctx: Ctx) -> bool:
        if ctx.role != "owner" and self.store.count_schedules(ctx.user_id) >= self.s.max_schedules_per_user:
            await self.tg.send_message(ctx.chat_id, f"You already have {self.s.max_schedules_per_user} schedules. "
                                                    "Remove one with /unschedule.")
            return False
        return True

    async def cmd_remind(self, ctx: Ctx, arg: str) -> None:
        when, _, text = arg.partition(" ")
        if not text.strip():
            await self.tg.send_message(ctx.chat_id, "Usage: /remind <10m|2h30m|14:30|2026-10-01T09:00> <text>")
            return
        try:
            at = parse_when(when, datetime.now(self.tz))
        except ValueError as e:
            await self.tg.send_message(ctx.chat_id, str(e))
            return
        if not await self._schedule_quota_ok(ctx):
            return
        sid = self.store.add_schedule(ctx.chat_id, ctx.user_id, "remind", when, text.strip(), at.timestamp())
        self._sched_wake.set()
        await self.tg.send_message(ctx.chat_id, f"⏰ Reminder #{sid} set for {self._fmt(at.timestamp())}.")

    async def cmd_every(self, ctx: Ctx, arg: str) -> None:
        spec, prompt = split_spec(arg)
        if not prompt.strip():
            await self.tg.send_message(ctx.chat_id, "Usage: /every <cron: m h dom mon dow | @daily | 2h> <prompt>\n"
                                                    "e.g. /every 0 9 * * 1-5 Summarise my calendar")
            return
        try:
            first = next_run(spec, datetime.now(self.tz))
        except ValueError as e:
            await self.tg.send_message(ctx.chat_id, f"Bad schedule: {e}")
            return
        name = self.backend_name(ctx.chat_id)
        if not self.s.backend_allowed(ctx.role, name):
            await self.tg.send_message(ctx.chat_id, f"Your role ({ctx.role}) can't use the {name} backend.")
            return
        if not await self._schedule_quota_ok(ctx):
            return
        sid = self.store.add_schedule(ctx.chat_id, ctx.user_id, "every", spec, prompt.strip(), first.timestamp())
        self._sched_wake.set()
        await self.tg.send_message(ctx.chat_id, f"🔁 #{sid} runs `{spec}` on this chat's backend. "
                                                f"Next: {self._fmt(first.timestamp())}.")

    async def cmd_schedules(self, ctx: Ctx, arg: str) -> None:
        items = self.store.list_schedules(ctx.chat_id)
        if not items:
            await self.tg.send_message(ctx.chat_id, "Nothing scheduled. See /remind and /every.")
            return
        lines = [f"{'⏰' if sc.kind == 'remind' else '🔁'} #{sc.id} "
                 + (f"at {self._fmt(sc.next_run)}" if sc.kind == "remind"
                    else f"{sc.spec}, next {self._fmt(sc.next_run)}, ran {sc.runs}x")
                 + f": {sc.text[:80]}" for sc in items]
        await self.tg.send_message(ctx.chat_id, "\n".join(lines) + "\n\nCancel with /unschedule <id>.")

    async def cmd_unschedule(self, ctx: Ctx, arg: str) -> None:
        sc = self.store.get_schedule(int(arg.lstrip("#"))) if arg.lstrip("#").isdigit() else None
        if sc is None or sc.chat_id != ctx.chat_id:
            await self.tg.send_message(ctx.chat_id, "No such schedule in this chat. See /schedules.")
            return
        if sc.user_id != ctx.user_id and not self._at_least(ctx, "admin"):
            await self.tg.send_message(ctx.chat_id, "Only its creator or an admin can cancel it.")
            return
        self.store.delete_schedule(sc.id)
        await self.tg.send_message(ctx.chat_id, f"Cancelled #{sc.id}.")

    async def scheduler(self) -> None:
        """Fire due schedules forever. Missed runs (bot was down) fire once, late."""
        while True:
            try:
                await self.run_due()
            except Exception:
                log.exception("scheduler tick failed")
            nxt = self.store.next_due()
            wait = 30.0 if nxt is None else min(30.0, max(0.2, nxt - time.time()))
            self._sched_wake.clear()
            try:
                await asyncio.wait_for(self._sched_wake.wait(), wait)
            except asyncio.TimeoutError:
                pass

    async def run_due(self) -> None:
        now = time.time()
        for sc in self.store.due_schedules(now):
            late = now - sc.next_run > 120
            role = self.role_of({"id": sc.user_id})
            if role is None:  # creator lost access
                self.store.delete_schedule(sc.id)
                log.info("dropped schedule %s: user %s no longer allowed", sc.id, sc.user_id)
                continue
            if sc.kind == "remind":
                self.store.delete_schedule(sc.id)
                note = " (late: the bot was offline)" if late else ""
                await self.tg.send_message(sc.chat_id, f"⏰ Reminder{note}: {sc.text}")
                continue
            if sc.chat_id in self._busy:  # try again shortly without counting a run
                self.store.reschedule(sc.id, now + 15, ran=False)
                continue
            try:
                nxt = next_run(sc.spec, datetime.now(self.tz)).timestamp()
            except ValueError as e:
                self.store.delete_schedule(sc.id)
                await self.tg.send_message(sc.chat_id, f"Removed schedule #{sc.id}: {e}")
                continue
            self.store.reschedule(sc.id, nxt)  # before running, so a crash can't replay it
            await self.tg.send_message(sc.chat_id, f"🔁 #{sc.id}: {sc.text[:300]}")
            await self._start_turn(Ctx(sc.chat_id, sc.user_id, role, {}), sc.text, [])
