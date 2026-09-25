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


async def test_reminder_survives_restart(tmp_path):
    fake = FakeTelegram()
    async with serve(fake.app) as url:
        s = Settings(telegram_token=fake.token, telegram_api_base=url, allowed_user_ids=frozenset({OWNER}),
                     db_path=str(tmp_path / "bot.db"), http_host="127.0.0.1", http_port=free_port())
        stop = asyncio.Event()
        task = asyncio.create_task(run(s, stop))
        fake.push_message(OWNER, "/remind 2s take the pizza out")
        await wait_for(lambda: any(t.startswith("⏰ Reminder #1 set") for t in fake.texts(OWNER)))
        stop.set()
        await asyncio.wait_for(task, 10)
        assert not any("pizza" in t for t in fake.texts(OWNER))

        stop = asyncio.Event()
        task = asyncio.create_task(run(s, stop))  # same DB, fresh process state
        await wait_for(lambda: "⏰ Reminder: take the pizza out" in fake.texts(OWNER))
        stop.set()
        await asyncio.wait_for(task, 10)


def test_client_ip_only_trusts_forwarded_for_from_trusted_proxies():
    import ipaddress

    from claw_telegram.app import client_ip
    from claw_telegram.config import _networks

    proxies = _networks("127.0.0.1, 10.0.0.0/8")
    assert str(client_ip("149.154.167.1", "1.2.3.4", proxies)) == "149.154.167.1"  # not a proxy: header ignored
    assert str(client_ip("127.0.0.1", "6.6.6.6, 149.154.167.1, 10.1.1.1", proxies)) == "149.154.167.1"
    assert client_ip("127.0.0.1", "garbage", proxies) is None
    assert ipaddress.ip_address("91.108.4.9") in _networks("telegram")[1]


async def test_webhook_ip_allowlist():
    from claw_telegram.config import _networks

    update = {"update_id": 1, "message": {"message_id": 1, "chat": {"id": OWNER, "type": "private"},
                                          "from": {"id": OWNER, "is_bot": False}, "text": "hi"}}
    headers = {"X-Telegram-Bot-Api-Secret-Token": "s3cret"}
    async with harness(mode="webhook", webhook_secret="s3cret", webhook_ip_allowlist=_networks("telegram"),
                       webhook_trusted_proxies=_networks("127.0.0.1")) as h:
        app = make_web_app(h.bot, h.bot.s)
        async with serve(app) as url, aiohttp.ClientSession() as http:
            async with http.post(url + WEBHOOK_PATH, json=update, headers=headers) as r:
                assert r.status == 403  # 127.0.0.1 is a proxy, no forwarded client
            async with http.post(url + WEBHOOK_PATH, json=update,
                                 headers=headers | {"X-Forwarded-For": "8.8.8.8"}) as r:
                assert r.status == 403
            async with http.post(url + WEBHOOK_PATH, json=update,
                                 headers=headers | {"X-Forwarded-For": "149.154.167.220"}) as r:
                assert r.status == 200
            await h.bot.drain()
        assert h.fake.texts(OWNER)[0].startswith("echo: hi")
