"""Общие действия с чатами, которые нужны разным обработчикам."""

from __future__ import annotations

from .config import Settings
from .db import Chat, Database
from .formatting import preview


async def create_chat(db: Database, user_id: int, settings: Settings, *, title: str = "Новый чат") -> Chat:
    cfg = settings.provider(settings.default_provider)
    if cfg is None:  # защита от кривой конфигурации
        raise RuntimeError("Нет доступного провайдера по умолчанию")
    is_deepseek = cfg.kind == "deepseek_web"
    model = "default" if is_deepseek else cfg.model
    # В новых чатах веб-поиск сразу включён (актуально только для DeepSeek).
    chat = await db.create_chat(
        user_id, provider=cfg.key, model=model, title=title, search=is_deepseek
    )
    await db.set_active_chat(user_id, chat.id)
    return chat


async def ensure_active_chat(db: Database, user_id: int, settings: Settings) -> Chat:
    chat = await db.active_chat(user_id)
    if chat is not None:
        return chat
    chats = await db.list_chats(user_id)
    if chats:
        await db.set_active_chat(user_id, chats[0].id)
        return chats[0]
    return await create_chat(db, user_id, settings)


def title_from_text(text: str) -> str:
    title = preview(text, 40)
    return title or "Новый чат"


def default_model_for(settings: Settings, provider_key: str) -> str:
    cfg = settings.provider(provider_key)
    if cfg is None:
        return ""
    return "default" if cfg.kind == "deepseek_web" else cfg.model
