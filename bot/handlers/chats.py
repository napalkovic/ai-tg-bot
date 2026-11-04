"""Чаты: создание, список, переключение, переименование, очистка, удаление."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from ..config import Settings
from ..db import Database
from ..keyboards import (
    BTN_CHATS,
    BTN_NEW,
    chats_keyboard,
    confirm_keyboard,
    main_keyboard,
)
from ..service import create_chat, ensure_active_chat

router = Router(name="chats")


async def _list_view(db: Database, user_id: int, page: int = 0):
    chats = await db.list_chats(user_id)
    active = await db.active_chat(user_id)
    if chats:
        header = (
            f"Активный чат: «{active.title}»\n\nВыбери другой или создай новый:"
            if active
            else "Выбери чат:"
        )
    else:
        header = "Чатов пока нет. Создай первый:"
    return header, chats_keyboard(chats, active.id if active else None, page=page)


@router.message(Command("new"))
@router.message(F.text == BTN_NEW)
async def cmd_new(message: Message, db: Database, settings: Settings) -> None:
    chat = await create_chat(db, message.from_user.id, settings)
    await message.answer(
        f"Создал чат «{chat.title}» 🆕\nПиши — история будет копиться здесь.",
        reply_markup=main_keyboard(),
    )


@router.message(Command("chats"))
@router.message(F.text == BTN_CHATS)
async def cmd_chats(message: Message, db: Database, settings: Settings) -> None:
    text, keyboard = await _list_view(db, message.from_user.id)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == "new")
async def cb_new(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    chat = await create_chat(db, callback.from_user.id, settings)
    await callback.answer("Готово")
    if callback.message:
        await callback.message.edit_text(
            f"Создал чат «{chat.title}» 🆕\nПиши — история будет копиться здесь.",
            reply_markup=None,
        )


@router.callback_query(F.data.startswith("chp:"))
async def cb_page(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    try:
        page = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer()
        return
    text, keyboard = await _list_view(db, callback.from_user.id, page)
    if callback.message:
        await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("ch:"))
async def cb_select(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    try:
        chat_id = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer()
        return
    chat = await db.get_chat(chat_id, callback.from_user.id)
    if chat is None:
        await callback.answer("Этот чат уже удалён", show_alert=True)
        return
    await db.set_active_chat(callback.from_user.id, chat.id)
    text, keyboard = await _list_view(db, callback.from_user.id)
    if callback.message:
        await callback.message.edit_text(
            f"✅ Активный чат: «{chat.title}»\n\nМожно писать.", reply_markup=keyboard
        )
    await callback.answer("Переключился")


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.message(Command("rename"))
async def cmd_rename(message: Message, command: CommandObject, db: Database, settings: Settings) -> None:
    title = (command.args or "").strip()
    if not title:
        await message.answer("Использование: /rename Новое название чата")
        return
    chat = await ensure_active_chat(db, message.from_user.id, settings)
    await db.update_chat(chat.id, title=title[:80])
    await message.answer(f"Переименовал в «{title[:80]}».")


@router.message(Command("clear"))
async def cmd_clear(message: Message, db: Database, settings: Settings) -> None:
    chat = await ensure_active_chat(db, message.from_user.id, settings)
    await message.answer(
        f"Очистить историю чата «{chat.title}»?\n"
        "Модель забудет всё, что было в этом чате.",
        reply_markup=confirm_keyboard("clear"),
    )


@router.message(Command("delete"))
async def cmd_delete(message: Message, db: Database, settings: Settings) -> None:
    chat = await ensure_active_chat(db, message.from_user.id, settings)
    await message.answer(
        f"Удалить чат «{chat.title}» вместе с историей?",
        reply_markup=confirm_keyboard("del"),
    )


@router.callback_query(F.data == "clear")
async def cb_clear(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    chat = await ensure_active_chat(db, callback.from_user.id, settings)
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            f"Очистить историю чата «{chat.title}»?",
            reply_markup=confirm_keyboard("clear"),
        )


@router.callback_query(F.data == "del")
async def cb_delete(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    chat = await ensure_active_chat(db, callback.from_user.id, settings)
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            f"Удалить чат «{chat.title}» вместе с историей?",
            reply_markup=confirm_keyboard("del"),
        )


@router.callback_query(F.data == "confirm:clear")
async def cb_confirm_clear(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    chat = await ensure_active_chat(db, callback.from_user.id, settings)
    await db.clear_chat(chat.id)
    await callback.answer("История очищена")
    if callback.message:
        await callback.message.edit_text(f"🧹 История чата «{chat.title}» очищена.")


@router.callback_query(F.data == "confirm:del")
async def cb_confirm_delete(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    chat = await db.active_chat(callback.from_user.id)
    if chat is None:
        await callback.answer("Чат уже удалён")
        return
    await db.delete_chat(chat.id)

    remaining = await db.list_chats(callback.from_user.id)
    if remaining:
        await db.set_active_chat(callback.from_user.id, remaining[0].id)
        note = f"Активным стал «{remaining[0].title}»."
    else:
        fresh = await create_chat(db, callback.from_user.id, settings)
        note = f"Создал новый чат «{fresh.title}»."

    await callback.answer("Удалено")
    if callback.message:
        await callback.message.edit_text(f"🗑 Чат «{chat.title}» удалён.\n{note}")


@router.callback_query(F.data == "close")
async def cb_close(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.delete()


@router.callback_query(F.data == "delall")
async def cb_delete_all(callback: CallbackQuery, db: Database) -> None:
    count = len(await db.list_chats(callback.from_user.id))
    if count == 0:
        await callback.answer("Чатов и так нет", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            f"⚠️ Удалить ВСЕ чаты ({count}) вместе с историей?\n"
            "Это действие нельзя отменить.",
            reply_markup=confirm_keyboard("delall"),
        )


@router.callback_query(F.data == "confirm:delall")
async def cb_confirm_delete_all(
    callback: CallbackQuery, db: Database, settings: Settings
) -> None:
    deleted = await db.delete_all_chats(callback.from_user.id)
    fresh = await create_chat(db, callback.from_user.id, settings)
    await callback.answer("Удалено")
    if callback.message:
        await callback.message.edit_text(
            f"🗑 Удалил чаты: {deleted}.\nСоздал новый чат «{fresh.title}» — он активен.",
            reply_markup=None,
        )
