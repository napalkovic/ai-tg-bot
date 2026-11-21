"""Мини-HTTP-сервер.

Нужен бесплатным хостингам (Hugging Face Spaces, Render и т.п.): они проверяют,
что приложение слушает порт, и по запросам понимают, что оно живое.
На обычном сервере/ноутбуке не запускается — порт не задан.
"""

from __future__ import annotations

import logging
import time

from aiohttp import web

log = logging.getLogger(__name__)


def build_app(bot_username: str, started_at: float) -> web.Application:
    app = web.Application()

    async def index(_: web.Request) -> web.Response:
        uptime = int(time.time() - started_at)
        return web.Response(
            text=f"AI Telegram bot @{bot_username} is running\nuptime: {uptime}s\n",
            content_type="text/plain",
        )

    async def health(_: web.Request) -> web.Response:
        return web.json_response(
            {"status": "ok", "uptime": int(time.time() - started_at)}
        )

    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    return app


async def start_health_server(port: int, bot_username: str) -> web.AppRunner:
    runner = web.AppRunner(build_app(bot_username, time.time()), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("HTTP-порт %s открыт (страница /health)", port)
    return runner
