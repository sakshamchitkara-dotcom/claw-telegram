"""Process wiring: long polling or webhook, plus /healthz and the optional /admin page."""

from __future__ import annotations

import asyncio
import base64
import binascii
import collections
import hmac
import html
import ipaddress
import logging
import time
from datetime import datetime

from aiohttp import web

from .backends import build_backends
from .bot import COMMANDS, Bot
from .config import Settings
from .failover import healthy
from .store import Store
from .telegram import Telegram, TelegramError
from .transcribe import Transcriber

log = logging.getLogger(__name__)
WEBHOOK_PATH = "/telegram/webhook"
STATS = web.AppKey("stats", dict)


def client_ip(remote: str | None, forwarded_for: str,
              trusted: tuple) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The caller's IP: the socket peer, or, when that is a trusted proxy, the right-most
    X-Forwarded-For entry that isn't one (entries left of it could be forged by the client)."""
    try:
        ip = ipaddress.ip_address(remote or "")
    except ValueError:
        return None
    hops = [h.strip() for h in forwarded_for.split(",") if h.strip()]
    while hops and any(ip in net for net in trusted):
        try:
            ip = ipaddress.ip_address(hops.pop())
        except ValueError:
            return None
    return ip


def basic_auth_ok(header: str, password: str) -> bool:
    """HTTP Basic credentials carry `password` (the user name is ignored). Constant-time compare."""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        given = base64.b64decode(token, validate=True).decode().partition(":")[2]
    except (binascii.Error, UnicodeDecodeError):
        return False
    return hmac.compare_digest(given.encode(), password.encode())


def admin_page(bot: Bot, settings: Settings, stats: dict) -> str:
    """One read-only HTML page: backends, circuits, activity, pending approvals, usage, audit log."""
    e = html.escape
    up = int(time.time() - bot.started)
    rows = []
    for name in bot.backends:
        status = bot.health.status.get(name, "not checked yet")
        mark = "ok" if healthy(status) else ("unknown" if name not in bot.health.status else "bad")
        rows.append(f"<tr><td>{e(name)}</td><td class={mark}>{e(bot.health.describe(name))}</td></tr>")
    pending = bot.store.pending_tasks()
    tasks = "".join(f"<tr><td>#{t.id}</td><td>{t.chat_id}</td><td>{e(t.backend)}</td>"
                    f"<td>{e(t.summary[:200])}</td></tr>" for t in pending) or "<tr><td colspan=4>none</td></tr>"
    today = bot._today()
    usage = "".join(f"<tr><td>{uid}</td><td>{n}</td><td>{cin:,}</td><td>{cout:,}</td></tr>"
                    for uid, n, cin, cout in bot.store.usage_by_user(today)) or "<tr><td colspan=4>none</td></tr>"
    audit = "".join(
        f"<tr><td>{datetime.fromtimestamp(a.ts, bot.tz):%Y-%m-%d %H:%M:%S}</td><td>{e(a.action)}</td>"
        f"<td>{a.user_id if a.user_id is not None else 'system'}</td><td>{a.chat_id if a.chat_id is not None else ''}"
        f"</td><td>{e(a.detail.splitlines()[0][:160] if a.detail else '')}</td></tr>"
        for a in bot.store.audit_log(30)) or "<tr><td colspan=5>empty</td></tr>"
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>claw-telegram admin</title>
<style>body{{font:14px system-ui,sans-serif;margin:16px;max-width:1000px}}table{{border-collapse:collapse;width:100%;
margin-bottom:20px}}td,th{{border-bottom:1px solid #ccc;padding:4px 8px;text-align:left;vertical-align:top}}
.ok{{color:#176b2c}}.bad{{color:#b3261e}}.unknown{{color:#777}}</style></head><body>
<h1>claw-telegram</h1>
<p>mode {e(settings.mode)} · up {up // 3600}h{up % 3600 // 60:02d}m · {stats["updates"]} updates ·
{len(bot._turns)} replies running · default backend {e(settings.default_backend)}
{(" · fallbacks " + e(", ".join(settings.fallback_backends))) if settings.fallback_backends else ""}</p>
<h2>Backends</h2><table>{"".join(rows)}</table>
<h2>Pending approvals</h2><table><tr><th>task</th><th>chat</th><th>backend</th><th>action</th></tr>{tasks}</table>
<h2>Usage on {e(today)}</h2><table><tr><th>user</th><th>replies</th><th>chars in</th><th>chars out</th></tr>
{usage}</table>
<h2>Audit log (latest 30)</h2><table><tr><th>time</th><th>action</th><th>by</th><th>chat</th><th>detail</th></tr>
{audit}</table></body></html>"""


def make_web_app(bot: Bot, settings: Settings) -> web.Application:
    app = web.Application(client_max_size=4 * 1024 * 1024)
    stats = {"updates": 0, "last_update": None, "duplicates": 0}
    app[STATS] = stats
    recent: collections.OrderedDict[int, None] = collections.OrderedDict()  # update_ids already accepted

    async def healthz(request: web.Request) -> web.Response:
        return web.json_response({
            "ok": True, "mode": settings.mode, "backends": sorted(bot.backends),
            "uptime_s": int(time.time() - bot.started), **stats,
        })

    async def webhook(request: web.Request) -> web.Response:
        if settings.webhook_ip_allowlist:
            ip = client_ip(request.remote, request.headers.get("X-Forwarded-For", ""),
                           settings.webhook_trusted_proxies)
            if ip is None or not any(ip in net for net in settings.webhook_ip_allowlist):
                log.warning("webhook call from %s rejected by WEBHOOK_IP_ALLOWLIST", ip or request.remote)
                return web.Response(status=403)
        given = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(given.encode(), settings.webhook_secret.encode()):
            log.warning("webhook call with bad secret from %s", request.remote)
            return web.Response(status=401)
        try:
            update = await request.json()
        except ValueError:
            return web.Response(status=400)
        if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
            return web.Response(status=400)
        if update["update_id"] in recent:  # Telegram redelivers when an ack was slow or lost
            stats["duplicates"] += 1
            return web.Response(text="ok")
        recent[update["update_id"]] = None
        if len(recent) > 1000:
            recent.popitem(last=False)
        stats["updates"] += 1
        stats["last_update"] = int(time.time())
        bot.submit(update)  # ack fast; Telegram retries slow webhooks
        return web.Response(text="ok")

    async def admin(request: web.Request) -> web.Response:
        if not basic_auth_ok(request.headers.get("Authorization", ""), settings.admin_password):
            log.warning("admin page: bad or missing credentials from %s", request.remote)
            return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="claw-telegram"'})
        return web.Response(text=admin_page(bot, settings, stats), content_type="text/html",
                            headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                     "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})

    app.router.add_get("/healthz", healthz)
    if settings.admin_password:
        app.router.add_get("/admin", admin)
    if settings.mode == "webhook":
        app.router.add_post(WEBHOOK_PATH, webhook)
    return app


async def poll(bot: Bot, tg: Telegram, stats: dict, stop: asyncio.Event) -> None:
    await tg.delete_webhook()
    offset = None
    backoff = 1
    while not stop.is_set():
        try:
            updates = await tg.get_updates(offset, timeout=25)
            backoff = 1
        except (TelegramError, OSError, asyncio.TimeoutError) as e:
            log.warning("getUpdates failed: %s; retrying in %ss", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            stats["updates"] += 1
            stats["last_update"] = int(time.time())
            bot.submit(update)  # a slow voice note in one chat doesn't hold up the others


async def run(settings: Settings, stop: asyncio.Event | None = None) -> None:
    stop = stop or asyncio.Event()
    backends = build_backends(settings)
    store = Store(settings.db_path)
    transcriber = (Transcriber(settings.transcribe_url, settings.transcribe_model, settings.transcribe_key)
                   if settings.transcribe_url else None)
    if not settings.allowed_user_ids:
        log.warning("ALLOWED_USER_IDS is empty: every user will be refused")
    async with Telegram(settings.telegram_token, settings.telegram_api_base) as tg:
        bot = Bot(settings, tg, store, backends, transcriber)
        await bot.init()
        for b in backends.values():
            if hasattr(b, "start"):
                b.start()
        try:
            await tg.set_commands(COMMANDS)
        except TelegramError as e:
            log.warning("setMyCommands failed: %s", e)
        scheduler = asyncio.create_task(bot.scheduler(), name="scheduler")
        prober = (asyncio.create_task(bot.health.run(settings.health_interval_s), name="health")
                  if settings.health_interval_s > 0 else None)
        app = make_web_app(bot, settings)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, settings.http_host, settings.http_port).start()
        log.info("http on %s:%s (healthz%s)", settings.http_host, settings.http_port,
                 ", webhook" if settings.mode == "webhook" else "")
        try:
            if settings.mode == "webhook":
                await tg.set_webhook(settings.webhook_url.rstrip("/") + WEBHOOK_PATH, settings.webhook_secret)
                log.info("webhook registered; waiting for updates")
                await stop.wait()
            else:
                log.info("long polling started; backends: %s", ", ".join(backends))
                poller = asyncio.create_task(poll(bot, tg, app[STATS], stop))
                await asyncio.wait([poller, asyncio.create_task(stop.wait())], return_when=asyncio.FIRST_COMPLETED)
                poller.cancel()
                if not poller.cancelled() and poller.done() and poller.exception():
                    raise poller.exception()
        finally:
            await bot.shutdown()
            scheduler.cancel()
            if prober:
                prober.cancel()
            await runner.cleanup()
            for b in backends.values():
                await b.close()
            if transcriber:
                await transcriber.close()
