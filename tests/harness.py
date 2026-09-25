"""Wire a Bot to the fake Telegram server for in-process tests."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace

from conftest import serve
from fake_telegram import FakeTelegram

from claw_telegram.backends.echo import EchoBackend
from claw_telegram.bot import Bot
from claw_telegram.config import Settings
from claw_telegram.store import Store
from claw_telegram.telegram import Telegram

OWNER = 1001
STRANGER = 666


class Harness:
    def __init__(self, fake: FakeTelegram, tg: Telegram, bot: Bot):
        self.fake, self.tg, self.bot = fake, tg, bot
        self.offset = None

    async def pump(self, drain: bool = True) -> None:
        """Deliver queued updates like the polling loop would, then wait for background turns."""
        updates = await self.tg.get_updates(self.offset, timeout=0)
        for u in updates:
            self.offset = u["update_id"] + 1
            await self.bot.handle_update(u)
        if drain:
            await self.bot.drain()


@asynccontextmanager
async def harness(backends=None, **settings):
    fake = FakeTelegram()
    async with serve(fake.app) as url:
        s = replace(Settings(telegram_token=fake.token, telegram_api_base=url,
                             allowed_user_ids=frozenset({OWNER}), stream_edit_interval_s=0), **settings)
        async with Telegram(s.telegram_token, s.telegram_api_base) as tg:
            bot = Bot(s, tg, Store(":memory:"), backends or {"echo": EchoBackend()})
            await bot.init()
            yield Harness(fake, tg, bot)
