"""A small fake of the Telegram Bot API for tests and local end-to-end runs.

It keeps chats in memory, long-polls getUpdates, and rejects what the real
API rejects in ways that matter to us: texts over 4096 chars and HTML that
uses unsupported or unbalanced tags.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from html.parser import HTMLParser

from aiohttp import web

ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a",
                "blockquote", "tg-spoiler", "span"}


class _Checker(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.error: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            self.error = f"unsupported start tag \"{tag}\""
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.error = f"unexpected end tag \"{tag}\""


def check_html(text: str) -> str | None:
    c = _Checker()
    c.feed(text)
    c.close()
    if c.error:
        return c.error
    return f"unclosed tag \"{c.stack[-1]}\"" if c.stack else None


def _strip(text: str) -> str:
    out: list[str] = []

    class P(HTMLParser):
        def handle_data(self, d):
            out.append(d)

    P(convert_charrefs=True).feed(text)
    return "".join(out)


class FakeTelegram:
    def __init__(self, token: str = "TEST:TOKEN", bot_username: str = "claw_test_bot"):
        self.token = token
        self.bot_username = bot_username
        self.calls: list[tuple[str, dict]] = []
        self.messages: dict[tuple[int, int], dict] = {}  # (chat_id, message_id) -> message
        self.files: dict[str, bytes] = {}
        self.webhook: dict | None = None
        self._updates: list[dict] = []
        self._update_ids = itertools.count(1)
        self._msg_ids = itertools.count(100)
        self._new_update = asyncio.Event()
        self.app = web.Application()
        self.app.router.add_post(f"/bot{token}/{{method}}", self._method)
        self.app.router.add_get(f"/file/bot{token}/{{path:.*}}", self._file)

    # ---- helpers used by tests --------------------------------------------

    def push_message(self, user_id: int, text: str | None = None, chat_id: int | None = None, **extra) -> dict:
        chat_id = chat_id or user_id
        msg = {"message_id": next(self._msg_ids), "date": int(time.time()),
               "chat": {"id": chat_id, "type": "private" if chat_id == user_id else "group"},
               "from": {"id": user_id, "is_bot": False, "first_name": f"user{user_id}"}, **extra}
        if text is not None:
            msg["text"] = text
            if text.startswith("/"):
                cmd = text.split()[0]
                msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(cmd)}]
        return self._push({"message": msg})

    def push_callback(self, user_id: int, message: dict, data: str) -> dict:
        return self._push({"callback_query": {"id": str(next(self._msg_ids)), "data": data, "message": message,
                                              "from": {"id": user_id, "is_bot": False, "first_name": "u"},
                                              "chat_instance": "x"}})

    def add_file(self, file_id: str, data: bytes) -> None:
        self.files[file_id] = data

    def _push(self, update: dict) -> dict:
        update["update_id"] = next(self._update_ids)
        self._updates.append(update)
        self._new_update.set()
        return update

    def sent(self, chat_id: int | None = None) -> list[dict]:
        return [m for (c, _), m in sorted(self.messages.items(), key=lambda kv: kv[0][1])
                if chat_id is None or c == chat_id]

    def texts(self, chat_id: int | None = None) -> list[str]:
        return [m["text"] for m in self.sent(chat_id)]

    def with_keyboard(self, chat_id: int) -> list[dict]:
        return [m for m in self.sent(chat_id) if m.get("reply_markup", {}).get("inline_keyboard")]

    # ---- Bot API ------------------------------------------------------------

    async def _method(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        params = await request.json() if request.can_read_body else {}
        self.calls.append((method, params))
        handler = getattr(self, f"_m_{method}", None)
        if handler is None:
            return self._err(404, "Not Found: method not found")
        return await handler(params)

    @staticmethod
    def _ok(result) -> web.Response:
        return web.json_response({"ok": True, "result": result})

    @staticmethod
    def _err(code: int, description: str) -> web.Response:
        return web.json_response({"ok": False, "error_code": code, "description": description}, status=code)

    def _validate(self, params: dict) -> str | None:
        text = params.get("text", "")
        if not text.strip():
            return "Bad Request: message text is empty"
        plain = text
        if params.get("parse_mode") == "HTML":
            if err := check_html(text):
                return f"Bad Request: can't parse entities: {err}"
            plain = _strip(text)
        return "Bad Request: message is too long" if len(plain) > 4096 else None

    async def _m_getMe(self, p):
        return self._ok({"id": 1, "is_bot": True, "first_name": "claw", "username": self.bot_username})

    async def _m_getUpdates(self, p):
        if self.webhook:
            return self._err(409, "Conflict: can't use getUpdates method while webhook is active")
        offset = p.get("offset") or 0
        self._updates = [u for u in self._updates if u["update_id"] >= offset]
        if not self._updates:
            self._new_update.clear()
            try:
                await asyncio.wait_for(self._new_update.wait(), timeout=min(p.get("timeout", 0), 2))
            except asyncio.TimeoutError:
                pass
        return self._ok(list(self._updates))

    async def _m_sendMessage(self, p):
        if err := self._validate(p):
            return self._err(400, err)
        msg = {"message_id": next(self._msg_ids), "chat": {"id": p["chat_id"]}, "text": p["text"],
               "date": int(time.time()), "from": {"id": 1, "is_bot": True},
               "parse_mode": p.get("parse_mode")}
        if p.get("reply_markup"):
            msg["reply_markup"] = p["reply_markup"]
        self.messages[(p["chat_id"], msg["message_id"])] = msg
        return self._ok(msg)

    async def _m_editMessageText(self, p):
        key = (p["chat_id"], p["message_id"])
        if key not in self.messages:
            return self._err(400, "Bad Request: message to edit not found")
        if err := self._validate(p):
            return self._err(400, err)
        msg = self.messages[key]
        if msg["text"] == p["text"] and msg.get("reply_markup") == p.get("reply_markup"):
            return self._err(400, "Bad Request: message is not modified")
        msg["text"] = p["text"]
        msg["parse_mode"] = p.get("parse_mode")
        msg.setdefault("edits", 0)
        msg["edits"] += 1
        if "reply_markup" in p:
            msg["reply_markup"] = p["reply_markup"]
        else:
            msg.pop("reply_markup", None)
        return self._ok(msg)

    async def _m_answerCallbackQuery(self, p):
        return self._ok(True)

    async def _m_sendChatAction(self, p):
        return self._ok(True)

    async def _m_setMyCommands(self, p):
        return self._ok(True)

    async def _m_getFile(self, p):
        fid = p["file_id"]
        if fid not in self.files:
            return self._err(400, "Bad Request: invalid file_id")
        return self._ok({"file_id": fid, "file_size": len(self.files[fid]), "file_path": f"files/{fid}"})

    async def _m_setWebhook(self, p):
        self.webhook = p
        return self._ok(True)

    async def _m_deleteWebhook(self, p):
        self.webhook = None
        return self._ok(True)

    async def _file(self, request: web.Request) -> web.Response:
        fid = request.match_info["path"].split("/")[-1]
        if fid not in self.files:
            return web.Response(status=404)
        return web.Response(body=self.files[fid])


async def main(host: str = "127.0.0.1", port: int = 8081) -> None:  # pragma: no cover - manual use
    fake = FakeTelegram()
    runner = web.AppRunner(fake.app)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    print(f"fake Telegram Bot API on http://{host}:{port} token={fake.token}")
    await asyncio.Event().wait()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
