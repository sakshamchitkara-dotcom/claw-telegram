import asyncio
import socket

import aiohttp
from conftest import serve
from fake_telegram import FakeTelegram
from harness import OWNER, harness

from claw_telegram.app import WEBHOOK_PATH, make_web_app, run
from claw_telegram.config import Settings


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def wait_for(pred, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met")


async def test_polling_end_to_end(tmp_path):
    fake = FakeTelegram()
    port = free_port()
    async with serve(fake.app) as url:
        s = Settings(telegram_token=fake.token, telegram_api_base=url, allowed_user_ids=frozenset({OWNER}),
                     db_path=str(tmp_path / "bot.db"), http_host="127.0.0.1", http_port=port)
        stop = asyncio.Event()
        task = asyncio.create_task(run(s, stop))
        fake.push_message(OWNER, "hello over polling")
        await wait_for(lambda: any("echo: hello over polling" in t for t in fake.texts(OWNER)))
        async with aiohttp.ClientSession() as http:
            async with http.get(f"http://127.0.0.1:{port}/healthz") as r:
                health = await r.json()
        stop.set()
        await asyncio.wait_for(task, 10)
    assert health["ok"] and health["mode"] == "polling" and health["updates"] == 1
    assert ("setMyCommands" in [m for m, _ in fake.calls]) and ("deleteWebhook" in [m for m, _ in fake.calls])


async def test_webhook_checks_secret_and_dispatches():
    async with harness(mode="webhook", webhook_secret="s3cret") as h:
        app = make_web_app(h.bot, h.bot.s)
        update = {"update_id": 1, "message": {"message_id": 1, "chat": {"id": OWNER, "type": "private"},
                                              "from": {"id": OWNER, "is_bot": False}, "text": "via webhook"}}
        async with serve(app) as url, aiohttp.ClientSession() as http:
            async with http.post(url + WEBHOOK_PATH, json=update) as r:
                assert r.status == 401
            async with http.post(url + WEBHOOK_PATH, json=update,
                                 headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}) as r:
                assert r.status == 401
            async with http.post(url + WEBHOOK_PATH, json=update,
                                 headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"}) as r:
                assert r.status == 200
            await h.bot.drain()
            async with http.get(url + "/healthz") as r:
                assert (await r.json())["updates"] == 1
        assert h.fake.texts(OWNER)[0].startswith("echo: via webhook")


async def test_webhook_mode_registers_with_telegram(tmp_path):
    fake = FakeTelegram()
    async with serve(fake.app) as url:
        s = Settings(telegram_token=fake.token, telegram_api_base=url, mode="webhook", webhook_secret="abc",
                     webhook_url="https://bot.example.com/", db_path=str(tmp_path / "b.db"),
                     http_host="127.0.0.1", http_port=free_port())
        stop = asyncio.Event()
        task = asyncio.create_task(run(s, stop))
        await wait_for(lambda: fake.webhook is not None)
        stop.set()
        await asyncio.wait_for(task, 10)
    assert fake.webhook["url"] == "https://bot.example.com/telegram/webhook"
    assert fake.webhook["secret_token"] == "abc"
