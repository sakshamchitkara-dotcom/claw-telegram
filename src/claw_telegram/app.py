"""Process wiring: long polling or webhook, plus a /healthz endpoint."""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import logging
import time

from aiohttp import web

from .backends import build_backends
from .bot import COMMANDS, Bot
from .config import Settings
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


def make_web_app(bot: Bot, settings: Settings) -> web.Application:
    app = web.Application(client_max_size=4 * 1024 * 1024)
    stats = {"updates": 0, "last_update": None}
    app[STATS] = stats

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
        stats["updates"] += 1
        stats["last_update"] = int(time.time())
        bot.spawn(bot.handle_update(update))  # ack fast; Telegram retries slow webhooks
        return web.Response(text="ok")

    app.router.add_get("/healthz", healthz)
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
            # ponytail: updates are handled in order; long agent turns already run in the background.
            await bot.handle_update(update)


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
            scheduler.cancel()
            if prober:
                prober.cancel()
            await runner.cleanup()
            for b in backends.values():
                await b.close()
            if transcriber:
                await transcriber.close()
