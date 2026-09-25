"""End-to-end run: real bot process <-> fake Telegram Bot API <-> a backend.

Starts the fake Telegram server in this process, launches `python -m claw_telegram`
as a subprocess pointed at it, plays a scripted conversation (approvals, failover
from a backend that hangs, /cancel, reminders, a group chat, pagination,
/summarize, /usage, export, the admin page) and prints the transcript as the users
would see it. Exits non-zero if an expected reply is missing.

    python scripts/e2e_fake_telegram.py                     # echo/mock backend
    python scripts/e2e_fake_telegram.py --backend openai \\
        --env OPENAI_BASE_URL=http://localhost:11434/v1 --env OPENAI_MODEL=hermes3:3b \\
        --say "Hello" --say "/status"
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import shutil
import socket
import sys
import tempfile
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from fake_telegram import FakeTelegram  # noqa: E402

OWNER, STRANGER, GROUP = 4242, 999, -100777
BOT = "claw_test_bot"


ADMIN_PASSWORD = "e2e-admin-password-0123"
FIRST_TOKEN_S = 3


def hung_backend() -> web.Application:
    """An OpenAI-compatible server that looks healthy, accepts chat requests, and then says nothing."""
    async def models(request):
        return web.json_response({"data": [{"id": "hung"}]})

    async def completions(request):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(b": accepted\n\n")
        await asyncio.sleep(3600)
        return resp

    app = web.Application()
    app.router.add_get("/v1/models", models)
    app.router.add_post("/v1/chat/completions", completions)
    return app


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def approval_prompts(fake: FakeTelegram, chat: int) -> list[dict]:
    return [m for m in fake.with_keyboard(chat)
            if m["reply_markup"]["inline_keyboard"][0][0]["callback_data"].startswith("ap:")]


async def wait_idle(fake: FakeTelegram, settle: float = 1.5, timeout: float = 600) -> None:
    """Wait until the bot is quiet for `settle` seconds and no turn is running,
    or until an approval prompt is waiting for the user."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last, quiet_since = len(fake.calls), loop.time()
    while loop.time() < deadline:
        await asyncio.sleep(0.1)
        if len(fake.calls) != last:
            last, quiet_since = len(fake.calls), loop.time()
            continue
        if loop.time() - quiet_since < settle:
            continue
        # "…" placeholder or a "⏳ status" line means an agent turn is still running
        running = [m for m in fake.sent() if m["text"] == "…" or m["text"].startswith("⏳")
                   or "\n\n⏳ " in m["text"]]
        if not running or any(approval_prompts(fake, c) for c in (OWNER, GROUP)):
            return
    raise TimeoutError("bot did not go idle")


def show(fake: FakeTelegram, since: int, chat: int = OWNER) -> int:
    msgs = fake.sent(chat)
    for m in msgs[since:]:
        kb = m.get("reply_markup", {}).get("inline_keyboard")
        extra = f"  [buttons: {' | '.join(b['text'] for b in kb[0])}]" if kb else ""
        mode = f" ({m['parse_mode']}, {m.get('edits', 0)} edits)" if m.get("parse_mode") or m.get("edits") else ""
        text = m["text"] if len(m["text"]) < 600 else f"{m['text'][:60]}... ({len(m['text'])} chars)"
        print(f"  bot{mode}: {text}{extra}".replace("\n", "\n       "))
    return len(msgs)


# (who, text) steps; who is "owner", "stranger", "group" (owner writing in a group), "wait" (seconds),
# or "nowait" (owner message; the next step follows 0.5 s later without waiting for the reply)
DEMO = [
    ("stranger", "hi"),
    ("owner", "/start"),
    ("owner", "Hello! What can you do?"),
    ("owner", "!task rm -rf ./build"),
    ("owner", "/tasks"),
    ("owner", "/backend openai"),
    ("owner", "Are you there?"),  # openai accepts, then goes silent -> first-token timeout -> echo
    ("owner", "/backend"),
    ("nowait", "Take your time with this one"),
    ("owner", "/cancel"),
    ("owner", "/backend echo"),
    ("owner", "x" * 4000),  # the echo is longer than one message -> Show more
    ("owner", "/remind 2s stand up"),
    ("wait", "3"),
    ("owner", "/every 0 9 * * 1-5 summarise my inbox"),
    ("owner", "/schedules"),
    ("group", "just chatting, not for the bot"),
    ("group", f"@{BOT} hello from the group"),
    ("owner", "/summarize"),
    ("owner", "/usage"),
    ("owner", "/export"),
    ("owner", "/audit"),
    ("owner", "/status"),
]
EXPECT = ["Not authorized", "Executed <code>rm -rf ./build</code>", "answered by echo; openai failed",
          f"openai: no response for {FIRST_TOKEN_S}s", "openai: ok (1 models, hung present)",
          f"⏹ Cancelled by {OWNER}.", "◀ Prev", "⏰ Reminder: stand up", "🔁 #2", "echo: hello from the group",
          "🗜 This summary replaced", "Today: ", "approve by user 4242"]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="echo")
    ap.add_argument("--env", action="append", default=[], help="extra KEY=VALUE for the bot process")
    ap.add_argument("--say", action="append", help="owner messages to send instead of the scripted demo")
    args = ap.parse_args()

    fake = FakeTelegram(bot_username=BOT)
    runner = web.AppRunner(fake.app)
    await runner.setup()
    tg_port = free_port()
    await web.TCPSite(runner, "127.0.0.1", tg_port).start()
    health_port = free_port()
    hung = web.AppRunner(hung_backend())
    await hung.setup()
    hung_port = free_port()
    await web.TCPSite(hung, "127.0.0.1", hung_port).start()
    db = tempfile.mkdtemp(prefix="claw-e2e-")
    env = {**os.environ, "TELEGRAM_BOT_TOKEN": fake.token, "TELEGRAM_API_BASE": f"http://127.0.0.1:{tg_port}",
           "ALLOWED_USER_IDS": str(OWNER), "DEFAULT_BACKEND": args.backend, "DB_PATH": f"{db}/bot.db",
           "HTTP_HOST": "127.0.0.1", "HTTP_PORT": str(health_port), "STREAM_EDIT_INTERVAL_S": "0.3",
           "LOG_LEVEL": "WARNING", "ALLOWED_GROUP_IDS": str(GROUP), "TIMEZONE": "UTC",
           "ADMIN_PASSWORD": ADMIN_PASSWORD}
    if not args.say:  # a hung OpenAI-compatible backend to fail over from
        env.update(OPENAI_BASE_URL=f"http://127.0.0.1:{hung_port}/v1", OPENAI_MODEL="hung",
                   FALLBACK_BACKENDS="echo", HEALTH_INTERVAL_S="0", FIRST_TOKEN_TIMEOUT_S=str(FIRST_TOKEN_S))
    env.update(kv.split("=", 1) for kv in args.env)
    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "claw_telegram", env=env, cwd=ROOT)
    print(f"bot pid {proc.pid}, fake Telegram on :{tg_port}, backend={args.backend}\n")
    steps = [("owner", t) for t in args.say] if args.say else DEMO
    try:
        await asyncio.sleep(1.5)
        seen = {OWNER: 0, STRANGER: 0, GROUP: 0}
        for who, text in steps:
            if who == "wait":
                print(f"\n(waiting {text}s)")
                await asyncio.sleep(float(text))
                await wait_idle(fake)
                seen[OWNER] = show(fake, seen[OWNER])
                continue
            if who == "nowait":
                print(f"\nowner {OWNER}: {text}")
                fake.push_message(OWNER, text)
                await asyncio.sleep(0.5)
                continue
            uid, chat = {"owner": (OWNER, OWNER), "stranger": (STRANGER, STRANGER), "group": (OWNER, GROUP)}[who]
            label = f"owner {OWNER} in group {GROUP}" if who == "group" else f"{who} {uid}"
            print(f"\n{label}: {text if len(text) < 200 else text[:40] + f'... ({len(text)} chars)'}")
            fake.push_message(uid, text, chat_id=chat)
            await wait_idle(fake)
            seen[chat] = show(fake, seen[chat], chat)
            for prompt in approval_prompts(fake, chat):
                data = prompt["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
                print(f"\nowner {OWNER}: [taps Approve -> {data}]")
                before = {id(m): m["text"] for m in fake.sent(chat)}
                fake.push_callback(OWNER, prompt, data)
                await wait_idle(fake)
                for m in fake.sent(chat):
                    if before.get(id(m)) not in (None, m["text"]):
                        print(f"  bot (edited): {m['text']}".replace("\n", "\n       "))
                seen[chat] = show(fake, seen[chat], chat)
            paged = [m for m in fake.sent(chat) if any(b["text"] == "Show more ▶" for b in
                                                     (m.get("reply_markup", {}).get("inline_keyboard") or [[]])[0])]
            for m in paged[-1:] if len(text) > 200 else []:
                data = m["reply_markup"]["inline_keyboard"][0][-1]["callback_data"]
                fake.push_callback(OWNER, m, data)
                await wait_idle(fake)
                print(f"\nowner {OWNER}: [taps Show more -> {data}]\n  bot (edited, page 2): ...{m['text'][-60:]!r}"
                      f"  [buttons: {' | '.join(b['text'] for b in m['reply_markup']['inline_keyboard'][0])}]")
        for d in fake.documents:
            body = d["data"].decode()
            summary = f"{len(json.loads(body)['messages'])} messages" if d["document"]["file_name"].endswith(
                ".json") else f"{len(body)} chars"
            print(f"\ndocument sent: {d['document']['file_name']} ({d['caption']}) -> {summary}")

        import aiohttp
        async with aiohttp.ClientSession() as http:
            async with http.get(f"http://127.0.0.1:{health_port}/healthz") as r:
                print(f"\nGET /healthz -> {r.status} {await r.text()}")
            admin_url = f"http://127.0.0.1:{health_port}/admin"
            async with http.get(admin_url) as r:
                print(f"GET /admin (no password) -> {r.status}")
            basic = "Basic " + base64.b64encode(f"admin:{ADMIN_PASSWORD}".encode()).decode()
            async with http.get(admin_url, headers={"Authorization": basic}) as r:
                page = await r.text()
                rows = re.findall(r"<tr><td>([^<]*)</td><td[^>]*>([^<]*)</td></tr>", page)
                print(f"GET /admin -> {r.status}; backends: " + "; ".join(f"{n}: {d}" for n, d in rows[:3]))
                admin_ok = r.status == 200 and "hung present" in page
    finally:
        proc.terminate()
        code = await proc.wait()
        print(f"bot exited with {code}")
        await runner.cleanup()
        await hung.cleanup()
        shutil.rmtree(db, ignore_errors=True)
    if not args.say:
        edits = [p.get("text", "") for m, p in fake.calls if m == "editMessageText"]  # incl. passing states
        everything = "\n".join([m["text"] for m in fake.sent()] + edits) + "\n" + "\n".join(
            b["text"] for m in fake.sent() for row in (m.get("reply_markup") or {}).get("inline_keyboard", [])
            for b in row)
        missing = [e for e in EXPECT if e not in everything]
        if missing or not fake.documents or not admin_ok:
            print(f"MISSING from transcript: {missing or ('export document' if not fake.documents else 'admin page')}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
