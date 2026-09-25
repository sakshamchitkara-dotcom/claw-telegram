"""Minimal async Telegram Bot API client (only the methods this bot uses)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

log = logging.getLogger(__name__)


class TelegramError(Exception):
    def __init__(self, method: str, code: int, description: str):
        super().__init__(f"{method}: {code} {description}")
        self.code = code
        self.description = description


class Telegram:
    def __init__(self, token: str, api_base: str = "https://api.telegram.org",
                 session: aiohttp.ClientSession | None = None):
        self._base = f"{api_base}/bot{token}"
        self._file_base = f"{api_base}/file/bot{token}"
        self._session = session
        self._own_session = session is None

    async def __aenter__(self) -> Telegram:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._own_session and self._session:
            await self._session.close()

    async def call(self, method: str, _http_timeout: float = 30, **params: Any) -> Any:
        payload = {k: v for k, v in params.items() if v is not None}
        for attempt in range(3):
            async with self._session.post(
                f"{self._base}/{method}", json=payload, timeout=aiohttp.ClientTimeout(total=_http_timeout)
            ) as resp:
                data = await resp.json(content_type=None)
            if data.get("ok"):
                return data["result"]
            code = data.get("error_code", resp.status)
            retry_after = (data.get("parameters") or {}).get("retry_after")
            if code == 429 and retry_after and attempt < 2:
                log.warning("telegram flood control on %s, sleeping %ss", method, retry_after)
                await asyncio.sleep(retry_after)
                continue
            raise TelegramError(method, code, data.get("description", ""))
        raise AssertionError("unreachable")

    async def get_me(self) -> dict:
        return await self.call("getMe")

    async def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict]:
        return await self.call("getUpdates", _http_timeout=timeout + 10, offset=offset, timeout=timeout,
                               allowed_updates=["message", "callback_query"])

    @staticmethod
    def _where(thread_id: int | None, reply_to: int | None) -> dict:
        """Forum topic and reply target; a reply whose target is gone is still sent."""
        out: dict[str, Any] = {"message_thread_id": thread_id or None}
        if reply_to:
            out["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        return out

    async def send_message(self, chat_id: int, text: str, parse_mode: str | None = None,
                           reply_markup: dict | None = None, thread_id: int | None = None,
                           reply_to: int | None = None) -> dict:
        return await self.call("sendMessage", chat_id=chat_id, text=text, parse_mode=parse_mode,
                               reply_markup=reply_markup, link_preview_options={"is_disabled": True},
                               **self._where(thread_id, reply_to))

    async def edit_message(self, chat_id: int, message_id: int, text: str,
                           parse_mode: str | None = None, reply_markup: dict | None = None) -> Any:
        try:
            return await self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                                   parse_mode=parse_mode, reply_markup=reply_markup,
                                   link_preview_options={"is_disabled": True})
        except TelegramError as e:
            if "message is not modified" in e.description:
                return None
            raise

    async def send_chat_action(self, chat_id: int, action: str = "typing", thread_id: int | None = None) -> None:
        try:
            await self.call("sendChatAction", chat_id=chat_id, action=action, message_thread_id=thread_id or None)
        except TelegramError:
            pass  # cosmetic

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        await self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    async def download_file(self, file_id: str, max_bytes: int = 20 * 1024 * 1024) -> bytes:
        info = await self.call("getFile", file_id=file_id)
        if info.get("file_size", 0) > max_bytes:
            raise TelegramError("getFile", 413, "file too large")
        async with self._session.get(f"{self._file_base}/{info['file_path']}") as resp:
            resp.raise_for_status()
            return await resp.read()

    async def set_webhook(self, url: str, secret: str) -> None:
        await self.call("setWebhook", url=url, secret_token=secret,
                        allowed_updates=["message", "callback_query"], drop_pending_updates=False)

    async def delete_webhook(self) -> None:
        await self.call("deleteWebhook")

    async def set_commands(self, commands: list[tuple[str, str]]) -> None:
        await self.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in commands])
