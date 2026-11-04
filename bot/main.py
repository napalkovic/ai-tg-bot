"""Точка входа: python -m bot"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message

from .config import ConfigError, Settings, load_settings
from .db import Database
from .handlers import setup_routers
from .health import start_health_server
from .imagegen import backend_label
from .providers import ProviderRegistry
from .uav import uav_poller

log = logging.getLogger("bot")


def _setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s  %(levelname)-7s %(name)-18s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def _owner_only(handler, event, data):
    settings: Settings = data["settings"]
    user = data.get("event_from_user")
    if user is not None and user.id in settings.owner_ids:
        return await handler(event, data)

    if isinstance(event, CallbackQuery):
        await event.answer("Это личный бот", show_alert=False)
    elif isinstance(event, Message):
        log.warning("Отказ доступа: %s (@%s)", user.id if user else "?", user.username if user else "?")
    return None


def _probe_deepseek(settings: Settings) -> None:
    """Проверяем, что сессия DeepSeek читается (запускается в отдельном потоке)."""
    from .providers.deepseek_web import load_session

    load_session(settings)


async def _session_guard(settings: Settings) -> None:
    """Раз в N минут обновляем токен DeepSeek из браузерного профиля."""
    if settings.deepseek_web is None or os.getenv("DEEPSEEK_TOKEN"):
        return
    if not (settings.session_dir / "profile").exists():
        # На сервере профиля браузера нет: работаем на токене из .env.
        log.info(
            "Профиль браузера не найден — автообновление сессии DeepSeek отключено"
        )
        return
    interval = max(15, settings.ds_refresh_minutes) * 60
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(_probe_deepseek, settings)
            log.info("Сессия DeepSeek обновлена")
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось обновить сессию DeepSeek: %s", exc)


async def main() -> None:
    _setup_logging()
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"\n❌ {exc}\n")
        sys.exit(1)

    db = Database(path=settings.db_path, url=settings.db_url)
    await db.connect()
    registry = ProviderRegistry(settings)
    log.info("База: %s", db.target)

    bot = Bot(
        settings.bot_token,
        default=DefaultBotProperties(parse_mode=None, link_preview_is_disabled=True),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(_owner_only)
    dp.callback_query.outer_middleware(_owner_only)
    setup_routers(dp)

    log.info("Источники ответов: %s", ", ".join(p.label for p in settings.available_providers()))
    log.info("По умолчанию: %s", settings.default_provider)
    log.info("Генерация картинок: %s", backend_label(settings) or "не настроена")
    if settings.fallback is None:
        log.info("Резервная модель не настроена (FALLBACK_PROVIDER/ключ)")

    if settings.deepseek_web is not None:
        try:
            await asyncio.to_thread(_probe_deepseek, settings)
            log.info("Сессия DeepSeek: OK")
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "Сессия DeepSeek недоступна: %s\n"
                "  → запусти ./scripts/ds_login.sh и войди в аккаунт.",
                exc,
            )

    guard = asyncio.create_task(_session_guard(settings))
    uav_task = asyncio.create_task(uav_poller(bot, db))
    health = None
    try:
        me = await bot.get_me()
        log.info("Бот @%s запущен", me.username)
        if settings.port:
            health = await start_health_server(settings.port, me.username or "bot")
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(
            bot,
            settings=settings,
            db=db,
            registry=registry,
            locks={},
        )
    finally:
        guard.cancel()
        uav_task.cancel()
        if health is not None:
            await health.cleanup()
        await registry.aclose()
        await db.close()
        await bot.session.close()


def run() -> None:
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    run()
