"""Старт и справка."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from ..config import Settings
from ..db import Database
from ..keyboards import main_keyboard
from ..service import ensure_active_chat

router = Router(name="common")


def _provider_line(settings: Settings, key: str) -> str:
    provider = settings.provider(key)
    return provider.label if provider else key


async def run_start(message: Message, db: Database, settings: Settings) -> None:
    """Приветствие /start — общее для команды и кнопки «Начать заново»."""
    chat = await ensure_active_chat(db, message.from_user.id, settings)
    await message.answer(
        "Привет! Я твой личный ИИ-ассистент.\n\n"
        "Просто напиши сообщение — отвечу в текущем чате. "
        "У каждого чата своя история и память, так что темы не смешиваются.\n\n"
        f"Чат: «{chat.title}»\n"
        f"Модель: {_provider_line(settings, chat.provider)}\n\n"
        "Кнопки внизу: новый чат, список чатов, настройки.\n"
        "Полный список команд — /help",
        reply_markup=main_keyboard(),
    )


@router.message(CommandStart())
async def cmd_start(message: Message, db: Database, settings: Settings) -> None:
    await run_start(message, db, settings)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "Что я умею:\n\n"
        "• Пиши любой текст — отвечу с учётом истории текущего чата.\n"
        "• Пришли фото или пересланный пост с фото — посмотрю и разберу.\n"
        "• Пересланный пост с видео: видео пропущу, а текст прочитаю.\n"
        "• Кинь ссылку на YouTube (можно Shorts) — посмотрю видео и перескажу.\n"
        "• Попроси «нарисуй …» или используй /image — пришлю картинку.\n"
        "• Попроси «сгенерируй видео …» или /video — пришлю короткий ролик.\n"
        "• Каждый чат помнит своё: можно вести несколько тем параллельно.\n"
        "• Длинные ответы приходят по мере генерации, не нужно ждать целиком.\n"
        "• 🎧 Кнопка «Сервисы»: найду и пришлю трек по названию или исполнителю.\n\n"
        "Команды:\n"
        "/new — новый чат\n"
        "/chats — список чатов\n"
        "/rename <название> — переименовать текущий чат\n"
        "/clear — очистить историю текущего чата\n"
        "/delete — удалить текущий чат\n"
        "/music <запрос> — найти и прислать трек\n"
        "/image <описание> — нарисовать картинку\n"
        "/video <описание> — сгенерировать короткое видео\n"
        "/settings — модель, DeepThink, веб-поиск\n"
        "/help — эта справка",
        reply_markup=main_keyboard(),
    )
