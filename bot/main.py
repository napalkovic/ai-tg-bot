"""Точка входа: python -m bot"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message

from .config import ConfigError, Settings, load_settings
from .db import Database
from .db_migrate import maybe_import_sqlite, set_fallback_marker
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


def _safe_reason(exc: BaseException) -> str:
    """Короткая причина для лога/сообщения без пароля от базы."""
    text = f"{type(exc).__name__}: {exc}"
    text = re.sub(r"postgres(?:ql)?://\S+", "<DATABASE_URL>", text)
    return " ".join(text.split())[:300]


async def _connect_db(settings: Settings) -> tuple[Database, str | None]:
    """Подключаем основную базу; если она недоступна — временная SQLite.

    Neon может упереться в бесплатную квоту (21 ноября 2026:
    «InsufficientResourcesError: … exceeded the quota») или моргнуть. Бот не
    должен из-за этого умирать: пробуем трижды, затем работаем на локальной
    SQLite, а владельцу уходит предупреждение.
    """
    primary = Database(path=settings.db_path, url=settings.db_url)
    last_error: BaseException | None = None
    for attempt in range(3):
        try:
            await primary.connect()
            return primary, None
        except Exception as exc:  # noqa: BLE001 - любые проблемы с базой
            last_error = exc
            log.warning(
                "База недоступна (попытка %s/3): %s", attempt + 1, _safe_reason(exc)
            )
            try:
                await primary.close()
            except Exception:  # noqa: BLE001
                pass
            if attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))

    assert last_error is not None
    if not settings.db_url:
        # SQLite и была основной — тут падать честно, подменять нечем.
        raise last_error

    log.error(
        "Основная база недоступна (%s) — работаю на временной SQLite: %s",
        _safe_reason(last_error),
        settings.db_path,
    )
    fallback = Database(path=settings.db_path)
    await fallback.connect()
    # Помечаем базу: когда основная оживёт, её чаты можно будет перенести.
    set_fallback_marker(fallback)
    return fallback, _safe_reason(last_error)


async def main() -> None:
    _setup_logging()
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"\n❌ {exc}\n")
        sys.exit(1)

    db, db_problem = await _connect_db(settings)
    registry = ProviderRegistry(settings)
    log.info("База: %s", db.target)

    # Вернулись на основную базу — переносим чаты, накопленные на временной
    # SQLite (если она есть и помечена как наш запасной вариант).
    if db_problem is None:
        try:
            await maybe_import_sqlite(settings, db)
        except Exception:  # noqa: BLE001 — импорт не должен мешать запуску
            log.exception("Не удалось перенести временную базу")

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
