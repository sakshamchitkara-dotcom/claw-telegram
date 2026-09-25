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
            for _ in range(2):  # the second delivery is a redelivery of the same update
                async with http.post(url + WEBHOOK_PATH, json=update,
                                     headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"}) as r:
                    assert r.status == 200
            async with http.post(url + WEBHOOK_PATH, json=[1, 2],
                                 headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"}) as r:
                assert r.status == 400
            await h.bot.drain()
            async with http.get(url + "/healthz") as r:
                health = await r.json()
                assert health["updates"] == 1 and health["duplicates"] == 1
        (reply,) = h.fake.texts(OWNER)
        assert reply.startswith("echo: via webhook")


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


async def test_admin_page_needs_the_password_and_escapes_content():
    import base64

    pw = "a-long-admin-password"
    async with harness(admin_password=pw, timezone="UTC") as h:
        h.bot.store.audit("user.add", user_id=OWNER, chat_id=OWNER, detail="<script>alert(1)</script>")
        h.fake.push_message(OWNER, "!task make <deploy>")
        await h.pump(drain=False)
        await asyncio.sleep(0.05)
        app = make_web_app(h.bot, h.bot.s)
        good = "Basic " + base64.b64encode(f"admin:{pw}".encode()).decode()
        bad = "Basic " + base64.b64encode(b"admin:wrong").decode()
        async with serve(app) as url, aiohttp.ClientSession() as http:
            for auth in (None, bad, "Bearer " + pw, "Basic !!!"):
                async with http.get(url + "/admin", headers={"Authorization": auth} if auth else {}) as r:
                    assert r.status == 401 and r.headers["WWW-Authenticate"].startswith("Basic")
            async with http.get(url + "/admin", headers={"Authorization": good}) as r:
                assert r.status == 200 and r.headers["Cache-Control"] == "no-store"
                page = await r.text()
        assert "<td>echo</td>" in page and "1 replies running" in page
        assert "run shell: make &lt;deploy&gt;" in page  # the pending approval
        assert "&lt;script&gt;" in page and "<script>" not in page
        await h.bot.shutdown(grace=1)


async def test_admin_page_is_off_without_a_password():
    async with harness() as h:
        async with serve(make_web_app(h.bot, h.bot.s)) as url, aiohttp.ClientSession() as http:
            async with http.get(url + "/admin") as r:
                assert r.status == 404


def test_admin_password_must_be_long():
    import pytest
    with pytest.raises(SystemExit, match="16 characters"):
        Settings.from_env({"TELEGRAM_BOT_TOKEN": "t", "ADMIN_PASSWORD": "short"})


async def test_metrics_in_prometheus_format():
    import base64
    import re

    from test_failover import Down

    from claw_telegram.backends.echo import EchoBackend

    pw = "a-long-admin-password"
    backends = {"echo": EchoBackend(), "down": Down()}
    async with harness(backends=backends, admin_password=pw, fallback_backends=("echo",)) as h:
        await h.bot.health.check("echo")
        for text in ("hello", "/backend down", "hi", "/backend echo"):  # hi: down fails, echo answers
            h.fake.push_message(OWNER, text)
            await h.pump()
        h.fake.push_message(OWNER, "!task deploy")
        await h.pump(drain=False)
        await asyncio.sleep(0.05)
        prompt = h.fake.with_keyboard(OWNER)[-1]
        h.fake.push_callback(OWNER, prompt, "ap:1:0")
        await h.pump()
        async with serve(make_web_app(h.bot, h.bot.s)) as url, aiohttp.ClientSession() as http:
            async with http.get(url + "/metrics") as r:
                assert r.status == 401
            auth = "Basic " + base64.b64encode(f"x:{pw}".encode()).decode()
            async with http.get(url + "/metrics", headers={"Authorization": auth}) as r:
                assert r.status == 200 and r.content_type == "text/plain"
                body = await r.text()
    samples = dict(re.findall(r"^(claw_\S+) (\S+)$", body, re.M))
    assert samples['claw_turns_total{outcome="ok"}'] == "2"  # hello, and the denied !task
    assert samples['claw_turns_total{outcome="fallback"}'] == "1"
    assert samples['claw_backend_failures_total{backend="down"}'] == "1"
    assert samples['claw_backend_replies_total{backend="echo"}'] == "3"
    assert samples['claw_approvals_total{outcome="denied"}'] == "1"
    assert samples['claw_turn_seconds_count{outcome="ok"}'] == "2"
    assert samples['claw_backend_up{backend="echo"}'] == "1"
    assert samples['claw_circuit_state{backend="echo",state="closed"}'] == "1"
    assert samples["claw_turns_running"] == "0" and samples["claw_updates_total"] == "0"
    assert "# TYPE claw_turn_seconds summary" in body and body.endswith("\n")
    for line in body.splitlines():  # every line is a comment or `name{labels} number`
        assert line.startswith("# ") or re.fullmatch(r'claw_\w+(\{[^}]*\})? -?[\d.e+]+', line), line
