"""Оповещения о БПЛА: /uav задаёт ОБЩИЙ канал, /uavon·/uavoff — ЛИЧНЫЕ."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, User

from ..config import Settings
from ..db import Database
from ..service import ensure_active_chat
from ..uav import (
    DEFAULT_CHANNEL,
    KV_CHANNEL,
    KV_LAST,
    channel_slug,
    get_level,
    level_line,
    poll_once,
    reset_level,
    skip_backlog,
    user_key,
)

router = Router(name="uav")
log = logging.getLogger(__name__)


class UavStates(StatesGroup):
    waiting_channel = State()  # ждём ссылку на канал после /uav


async def _reset_watermark(db: Database) -> None:
    """Канал сменили — сбрасываем «прочитано», чтобы не сыпать старыми постами."""
    await db.kv_delete(KV_LAST)
    # Уровень относился к прежнему каналу — забываем, новый прочитаем заново.
    await reset_level(db)


async def _forget_level_branches(db: Database, user_id: int) -> None:
    """Выключил оповещения → стираем и «серверную» память веток DeepSeek.

    DeepSeek хранит ветку у себя и помнит вставки про уровень из прошлых
    запросов, поэтому одной фильтрации локальной истории мало. Начинаем ветки
    заново (`ds_cid = None`): локальная история остаётся, а лишнего ИИ уже не
    помнит.
    """
    try:
        for chat in await db.list_chats(user_id):
            if chat.ds_cid:
                await db.update_chat(chat.id, ds_cid=None)
    except Exception:  # noqa: BLE001 — выключение не должно падать
        log.exception("UAV: не удалось сбросить ветки DeepSeek после выключения")


@router.message(Command("uav"))
async def cmd_uav(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    db: Database,
    event_from_user: User,
) -> None:
    """Секретная команда: /uav <ссылка> — общий канал; /uav без аргумента — статус."""
    await state.clear()
    args = (command.args or "").strip()
    if args:
        slug = channel_slug(args)
        if not slug:
            await message.answer(
                "Так не пойдёт: нужна ссылка на ПУБЛИЧНЫЙ канал вида "
                "https://t.me/имя_канала (закрытые с +хэш читать нечем)."
            )
            return
        await db.kv_set(KV_CHANNEL, slug)
        await _reset_watermark(db)
        await message.answer(
            f"✅ Канал для ВСЕХ пользователей — @{slug} (применён сразу).\n"
            "Каждый решает сам, получать ли посты: /uavon или кнопка "
            "«🔴 Включить оповещения» в Настройках.\nВыключить себе: /uavoff"
        )
        return

    my_on = (await db.kv_get(user_key(event_from_user.id))) == "1"
    if not args:
        # Ждём ссылку ВСЕГДА: команда /uav без аргумента и описание, и вход
        # в диалог. Иначе, когда канал уже задан, присланный позже адрес
        # уходит в AI-чат (баг замечен владельцем 4 ноября 2026).
        await state.set_state(UavStates.waiting_channel)
    level = await get_level(db) if my_on else None
    level_text = (
        f"Уровень сейчас: {level_line(level)}\n"
        if my_on
        else "Уровень не показываю: оповещения выключены.\n"
    )
    await message.answer(
        "🚨 Оповещения о БПЛА\n\n"
        f"Канал: @{await db.kv_get(KV_CHANNEL) or DEFAULT_CHANNEL} (общий).\n"
        f"Твоё оповещение: {'🟢 включено' if my_on else '🔴 выключено'}.\n"
        f"{level_text}\n"
        "Сменить канал — пришли ссылку на публичный канал (t.me/имя). "
        "Включить себе: /uavon · Выключить: /uavoff"
    )


@router.message(UavStates.waiting_channel, F.text)
async def uav_channel_link(
    message: Message, state: FSMContext, db: Database
) -> None:
    text = (message.text or "").strip()
    slug = None if text.startswith("/") else channel_slug(text)
    await state.clear()
    if slug:
        await db.kv_set(KV_CHANNEL, slug)
        await _reset_watermark(db)
        await message.answer(
            f"✅ Канал для ВСЕХ — @{slug} (применён сразу).\n"
            "Каждый решает сам, получать ли посты: /uavon · /uavoff."
        )
        return
    await message.answer(
        "Это не похоже на ссылку канала. Попробуй ещё раз: /uav "
        "(пример: https://t.me/Lipetsk_radar)"
    )


@router.message(Command("uavon"))
async def cmd_uavon(
    message: Message,
    state: FSMContext,
    db: Database,
    event_from_user: User,
    bot: Bot,
) -> None:
    await state.clear()
    slug = await db.kv_get(KV_CHANNEL) or DEFAULT_CHANNEL
    await db.kv_set(user_key(event_from_user.id), "1")
    await message.answer(
        f"🟢 Оповещения тебе включены (канал @{slug}).\n"
        "Первые новые посты придут через 10–20 секунд. "
        "Отключить себе: /uavoff"
    )
    # Посты, вышедшие за время выключения, НЕ пересылаем: планку — на
    # самый свежий пост канала прямо сейчас.
    try:
        await skip_backlog(db)
    except Exception:  # noqa: BLE001
        log.exception("UAV: не перенёс планку при включении")
    # Не ждём полминуты — один цикл сразу (разослать «включившимся» актуальное).
    try:
        await poll_once(bot, db)
    except Exception:  # noqa: BLE001
        log.exception("Первый цикл UAV не удался")


@router.message(Command("uavoff"))
async def cmd_uavoff(
    message: Message, state: FSMContext, db: Database, event_from_user: User
) -> None:
    await state.clear()
    await db.kv_set(user_key(event_from_user.id), "0")
    await _forget_level_branches(db, event_from_user.id)
    await message.answer("🔴 Оповещения тебе выключены. Тишина.")


@router.callback_query(F.data == "uav:toggle")
async def cb_uav_toggle(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
    bot: Bot,
    state: FSMContext,
    event_from_user: User,
) -> None:
    """Кнопка в Настройках: личный включатель (канал общий)."""
    await callback.answer()
    my_on = (await db.kv_get(user_key(event_from_user.id))) == "1"
    await state.clear()
    new_on = not my_on
    await db.kv_set(user_key(event_from_user.id), "1" if new_on else "0")
    if not new_on:
        await _forget_level_branches(db, event_from_user.id)
    await callback.answer("🟢 Оповещения включены" if new_on else "🔴 Оповещения выключены")

    # Перерисовываем окно Настроек сразу — иначе непонятно, что клик сработал.
    chat = await ensure_active_chat(db, event_from_user.id, settings)
    if callback.message and callback.message.text:
        from .settings import _providers, _view  # локально: без цикла импортов
        from ..keyboards import settings_keyboard

        try:
            await callback.message.edit_text(
                _view(chat, settings),
                reply_markup=settings_keyboard(
                    chat,
                    providers=_providers(settings),
                    uav_enabled=new_on,
                ),
            )
        except TelegramBadRequest:
            pass

    if new_on:
        # Посты за время выключения не пересылаем: планка — на свежий пост.
        try:
            await skip_backlog(db)
        except Exception:  # noqa: BLE001
            log.exception("UAV: не перенёс планку при включении")
        # Проверяем канал немедленно — иначе первые ~15 секунд молчим.
        try:
            await poll_once(bot, db)
        except Exception:  # noqa: BLE001
            log.exception("Первый цикл UAV не удался")


@router.callback_query(F.data == "uav:off")
async def cb_uav_off(
    callback: CallbackQuery,
    db: Database,
    state: FSMContext,
    event_from_user: User,
) -> None:
    """Кнопка «🔕 Отключить оповещения» в пришедших постах (личная)."""
    await callback.answer()
    await state.clear()
    await db.kv_set(user_key(event_from_user.id), "0")
    await _forget_level_branches(db, event_from_user.id)
    if callback.message:
        try:
            if callback.message.text:
                await callback.message.edit_text("🔕 Оповещения выключены.")
            else:
                await callback.message.reply("🔕 Оповещения выключены.")
        except Exception:  # noqa: BLE001
            try:
                await callback.message.reply("🔕 Оповещения выключены.")
            except Exception:
                pass
