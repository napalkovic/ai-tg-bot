"""Настройки текущего чата: провайдер, модель, DeepThink, веб-поиск."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..config import DEEPSEEK_WEB, Settings
from ..db import Chat, Database
from ..imagegen import backend_label
from ..keyboards import BTN_SETTINGS, settings_keyboard
from ..service import default_model_for, ensure_active_chat
from .common import run_start
from ..uav import user_key

router = Router(name="settings")
log = logging.getLogger(__name__)


def _providers(settings: Settings) -> list[tuple[str, str]]:
    return [(p.key, p.label) for p in settings.available_providers()]


def _view(
    chat: Chat,
    settings: Settings,
) -> str:
    provider = settings.provider(chat.provider)
    lines = [f"⚙️ Чат «{chat.title}»", ""]
    lines.append(f"Провайдер: {provider.label if provider else chat.provider}")

    if chat.provider == DEEPSEEK_WEB:
        lines.append(f"Модель: {'🧠 Expert' if chat.model == 'expert' else '⚡ Instant'}")
        lines.append(f"DeepThink (рассуждения): {'вкл' if chat.thinking else 'выкл'}")
        lines.append(f"Веб-поиск: {'вкл' if chat.search else 'выкл'}")
    else:
        lines.append(f"Модель: {provider.model or chat.model or 'по умолчанию'}")

    vision = settings.vision_config()
    if vision is not None:
        lines.append(f"Картинки смотрю: {vision.label}")
    art = backend_label(settings)
    if art is not None:
        line = f"Рисую: {art}"
        if art == "Pollinations":
            line += " (запасной — добавь Cloudflare, будет красивее)"
        lines.append(line)

    if len(_providers(settings)) < 2:
        lines += ["", "Резервная модель не настроена — добавь ключ в .env, "
                      "чтобы переключаться, если DeepSeek недоступен."]
    lines += ["", "Изменения действуют только на этот чат."]
    return "\n".join(lines)


async def _render(
    callback: CallbackQuery,
    chat: Chat,
    settings: Settings,
    db: Database | None = None,
) -> None:
    uav_enabled = False
    if db is not None:
        uav_enabled = (
            (await db.kv_get(user_key(callback.from_user.id))) == "1"
        )
    if callback.message:
        await callback.message.edit_text(
            _view(chat, settings),
            reply_markup=settings_keyboard(
                chat,
                providers=_providers(settings),
                uav_enabled=uav_enabled,
            ),
        )


@router.message(Command("settings"))
@router.message(F.text == BTN_SETTINGS)
async def cmd_settings(message: Message, db: Database, settings: Settings) -> None:
    chat = await ensure_active_chat(db, message.from_user.id, settings)
    uav_enabled = (await db.kv_get(user_key(message.from_user.id))) == "1"
    await message.answer(
        _view(chat, settings),
        reply_markup=settings_keyboard(
            chat,
            providers=_providers(settings),
            uav_enabled=uav_enabled,
        ),
    )


@router.callback_query(F.data == "settings")
async def cb_settings(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    chat = await ensure_active_chat(db, callback.from_user.id, settings)
    await _render(callback, chat, settings, db=db)
    await callback.answer()


@router.callback_query(F.data.startswith("prov:"))
async def cb_provider(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    key = (callback.data or "").split(":", 1)[1]
    chat = await ensure_active_chat(db, callback.from_user.id, settings)
    if settings.provider(key) is None:
        await callback.answer("Этот провайдер не настроен", show_alert=True)
        return
    if key != chat.provider:
        # Ветка DeepSeek привязана к провайдеру: при смене начинаем новую,
        # история при этом не теряется — она хранится локально.
        chat = await db.update_chat(
            chat.id, provider=key, model=default_model_for(settings, key), ds_cid=None
        )
    await _render(callback, chat, settings)
    await callback.answer("Провайдер обновлён")


@router.callback_query(F.data.startswith("dsm:"))
async def cb_model(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    model = (callback.data or "").split(":", 1)[1]
    if model not in {"default", "expert"}:
        await callback.answer()
        return
    chat = await ensure_active_chat(db, callback.from_user.id, settings)
    # У ветки DeepSeek модель фиксируется при создании — начинаем новую.
    chat = await db.update_chat(chat.id, model=model, ds_cid=None)
    await _render(callback, chat, settings)
    await callback.answer("Модель выбрана")


@router.callback_query(F.data.startswith("toggle:"))
async def cb_toggle(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    field = (callback.data or "").split(":", 1)[1]
    if field not in {"thinking", "search"}:
        await callback.answer()
        return
    chat = await ensure_active_chat(db, callback.from_user.id, settings)
    chat = await db.update_chat(chat.id, **{field: not getattr(chat, field)})
    await _render(callback, chat, settings)
    await callback.answer("Готово")


@router.callback_query(F.data == "bot:reset")
async def cb_bot_reset(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    settings: Settings,
) -> None:
    """Кнопка «Начать заново»: ровно то же, что прислать /start."""
    await state.clear()
    await callback.answer("Готово")
    if callback.message:
        try:
            await run_start(callback.message, db, settings)
        except Exception:  # noqa: BLE001
            pass

