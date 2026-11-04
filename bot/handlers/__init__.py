"""Сборка роутеров."""

from __future__ import annotations

from aiogram import Dispatcher

from . import chat, chats, common, services, settings, uav


def setup_routers(dp: Dispatcher) -> None:
    # Порядок важен: команды и кнопки должны перехватываться раньше,
    # чем общий обработчик текста.
    dp.include_router(common.router)
    dp.include_router(uav.router)
    dp.include_router(chats.router)
    dp.include_router(services.router)
    dp.include_router(settings.router)
    dp.include_router(chat.router)


__all__ = ["setup_routers"]
