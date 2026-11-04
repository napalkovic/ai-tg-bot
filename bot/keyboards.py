"""Клавиатуры и кнопки."""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from .db import Chat

BTN_NEW = "➕ Новый чат"
BTN_CHATS = "🗂 Мои чаты"
BTN_SETTINGS = "⚙️ Настройки"
BTN_SERVICES = "🎧 Сервисы"

REPLY_BUTTONS = {BTN_NEW, BTN_CHATS, BTN_SETTINGS, BTN_SERVICES}

PAGE_SIZE = 7


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_NEW)],
            [KeyboardButton(text=BTN_CHATS), KeyboardButton(text=BTN_SETTINGS)],
            [KeyboardButton(text=BTN_SERVICES)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Напиши сообщение…",
    )


def _short(title: str, limit: int = 38) -> str:
    title = " ".join(title.split())
    return title if len(title) <= limit else title[: limit - 1].rstrip() + "…"


def chats_keyboard(
    chats: list[Chat],
    active_id: int | None,
    *,
    page: int = 0,
) -> InlineKeyboardMarkup:
    total_pages = max(1, (len(chats) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    window = chats[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

    rows: list[list[InlineKeyboardButton]] = []
    for chat in window:
        mark = "✅ " if chat.id == active_id else ""
        rows.append(
            [InlineKeyboardButton(text=f"{mark}{_short(chat.title)}", callback_data=f"ch:{chat.id}")]
        )

    nav: list[InlineKeyboardButton] = []
    if total_pages > 1:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"chp:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"chp:{page + 1}"))
        rows.append(nav)

    rows.append(
        [
            InlineKeyboardButton(text="➕ Новый чат", callback_data="new"),
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings"),
        ]
    )
    rows.append(
        [InlineKeyboardButton(text="🗑 Удалить все чаты", callback_data="delall")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_keyboard(
    chat: Chat,
    *,
    providers: list[tuple[str, str]],
    uav_enabled: bool = False,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    if len(providers) > 1:
        for key, label in providers:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=("✅ " if chat.provider == key else "") + label,
                        callback_data=f"prov:{key}",
                    )
                ]
            )

    is_deepseek = chat.provider == "deepseek_web"
    if is_deepseek:
        rows.append(
            [
                InlineKeyboardButton(
                    text=("✅ " if chat.model != "expert" else "") + "⚡ Instant",
                    callback_data="dsm:default",
                ),
                InlineKeyboardButton(
                    text=("✅ " if chat.model == "expert" else "") + "🧠 Expert",
                    callback_data="dsm:expert",
                ),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text=("✅ " if chat.thinking else "▫️ ") + "DeepThink",
                    callback_data="toggle:thinking",
                ),
                InlineKeyboardButton(
                    text=("✅ " if chat.search else "▫️ ") + "Веб-поиск",
                    callback_data="toggle:search",
                ),
            ]
        )

    # Оповещения о БПЛА (новый отключатель — отдельная строка).
    rows.append(
        [
            InlineKeyboardButton(
                text=(
                    "🟢 Выключить оповещения о БПЛА"
                    if uav_enabled
                    else "🔴 Включить оповещения о БПЛА"
                ),
                callback_data="uav:toggle",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(text="🧹 Очистить", callback_data="clear"),
            InlineKeyboardButton(text="🗑 Удалить чат", callback_data="del"),
        ]
    )
    # Пользовательский «перезапуск»: новый чат + сброс диалоговых состояний
    # (то, что владелец имел в виду под «перезапустить как /start»).
    rows.append(
        [
            InlineKeyboardButton(
                text="🔄 Начать заново", callback_data="bot:reset"
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="✖️ Закрыть", callback_data="close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_keyboard(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, уверен", callback_data=f"confirm:{action}"),
                InlineKeyboardButton(text="Отмена", callback_data="close"),
            ]
        ]
    )


def services_keyboard() -> InlineKeyboardMarkup:
    """Меню раздела «Сервисы» — со временем будем пополнять."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎵 Музыка", callback_data="svc:music")],
            [InlineKeyboardButton(text="🎬 ТикТок", callback_data="svc:tiktok")],
            [InlineKeyboardButton(text="🎞 Сгенерировать видео", callback_data="svc:video")],
            [InlineKeyboardButton(text="✖️ Закрыть", callback_data="svc:close")],
        ]
    )


def music_choices_keyboard(titles: list[str], *, limit: int = 38) -> InlineKeyboardMarkup:
    """Список найденных треков: пользователь выбирает кнопку."""
    rows: list[list[InlineKeyboardButton]] = []
    for index, title in enumerate(titles):
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{index + 1}. {_short(title, limit)}",
                    callback_data=f"mus:{index}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="✖️ Отмена", callback_data="mus:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def music_source_keyboard() -> InlineKeyboardMarkup:
    """«Где качаем?» — выбор источника музыки (шаг 1 сценария музыки)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎧 SoundCloud", callback_data="mus:src:sc")],
            [InlineKeyboardButton(text="🇷🇺 ВКонтакте", callback_data="mus:src:vk")],
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="mus:cancel")],
        ]
    )
