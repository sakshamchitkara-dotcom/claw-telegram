"""End-to-end run: real bot process <-> fake Telegram Bot API <-> a backend.

Starts the fake Telegram server in this process, launches `python -m claw_telegram`
as a subprocess pointed at it, plays a scripted conversation (including an
approval click) and prints the transcript as the user would see it.

    python scripts/e2e_fake_telegram.py                     # echo/mock backend
    python scripts/e2e_fake_telegram.py --backend openai \\
        --env OPENAI_BASE_URL=http://localhost:11434/v1 --env OPENAI_MODEL=hermes3:3b
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
import tempfile
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from fake_telegram import FakeTelegram  # noqa: E402

OWNER, STRANGER = 4242, 999


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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
        running = [m for m in fake.sent(OWNER) if m["text"] == "…" or m["text"].startswith("⏳")
                   or "\n\n⏳ " in m["text"]]
        if not running or fake.with_keyboard(OWNER):
            return
    raise TimeoutError("bot did not go idle")


def show(fake: FakeTelegram, since: int, chat: int = OWNER) -> int:
    msgs = fake.sent(chat)
    for m in msgs[since:]:
        kb = m.get("reply_markup", {}).get("inline_keyboard")
        extra = f"  [buttons: {' | '.join(b['text'] for b in kb[0])}]" if kb else ""
        mode = f" ({m['parse_mode']}, {m.get('edits', 0)} edits)" if m.get("parse_mode") or m.get("edits") else ""
        print(f"  bot{mode}: {m['text']}{extra}".replace("\n", "\n       "))
    return len(msgs)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="echo")
    ap.add_argument("--env", action="append", default=[], help="extra KEY=VALUE for the bot process")
    ap.add_argument("--say", action="append", help="messages to send (default: scripted demo)")
    args = ap.parse_args()

    fake = FakeTelegram()
    runner = web.AppRunner(fake.app)
    await runner.setup()
    tg_port = free_port()
    await web.TCPSite(runner, "127.0.0.1", tg_port).start()
    health_port = free_port()
    db = tempfile.mkdtemp(prefix="claw-e2e-")
    env = {**os.environ, "TELEGRAM_BOT_TOKEN": fake.token, "TELEGRAM_API_BASE": f"http://127.0.0.1:{tg_port}",
           "ALLOWED_USER_IDS": str(OWNER), "DEFAULT_BACKEND": args.backend, "DB_PATH": f"{db}/bot.db",
           "HTTP_HOST": "127.0.0.1", "HTTP_PORT": str(health_port), "STREAM_EDIT_INTERVAL_S": "0.3",
           "LOG_LEVEL": "WARNING"}
    env.update(kv.split("=", 1) for kv in args.env)
    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "claw_telegram", env=env, cwd=ROOT)
    print(f"bot pid {proc.pid}, fake Telegram on :{tg_port}, backend={args.backend}\n")
    try:
        await asyncio.sleep(1.5)
        seen = {OWNER: 0, STRANGER: 0}
        script = args.say or ["/start", "Hello! What can you do?", "!task rm -rf ./build", "/tasks", "/status"]

        print(f"stranger {STRANGER}: hi")
        fake.push_message(STRANGER, "hi")
        await wait_idle(fake)
        seen[STRANGER] = show(fake, 0, STRANGER)

        for text in script:
            print(f"\nowner {OWNER}: {text}")
            fake.push_message(OWNER, text)
            await wait_idle(fake)
            seen[OWNER] = show(fake, seen[OWNER])
            pending = [m for m in fake.with_keyboard(OWNER)]
            for prompt in pending:
                data = prompt["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
                print(f"\nowner {OWNER}: [taps Approve -> {data}]")
                before = {id(m): m["text"] for m in fake.sent(OWNER)}
                fake.push_callback(OWNER, prompt, data)
                await wait_idle(fake)
                for m in fake.sent(OWNER):
                    if before.get(id(m)) not in (None, m["text"]):
                        print(f"  bot (edited): {m['text']}".replace("\n", "\n       "))
                seen[OWNER] = show(fake, seen[OWNER])

        import aiohttp
        async with aiohttp.ClientSession() as http, http.get(f"http://127.0.0.1:{health_port}/healthz") as r:
            print(f"\nGET /healthz -> {r.status} {await r.text()}")
    finally:
        proc.terminate()
        code = await proc.wait()
        print(f"bot exited with {code}")
        await runner.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
