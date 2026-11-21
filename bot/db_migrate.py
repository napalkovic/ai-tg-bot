"""Перенос чатов из временной SQLite в основную базу (Neon).

Зачем: 21 ноября 2026 бесплатный Neon исчерпал квоту, и бот переехал на
временную SQLite (`bot/main.py::_connect_db`). Когда квота восстановится и
бот снова подключится к Postgres, накопленные за это время чаты жалко
терять — этот модуль один раз копирует их в основную базу и переименовывает
временный файл, чтобы не импортировать его повторно.

Импортируем ТОЛЬКО чаты и сообщения (и активный чат). Настройки/токены в kv
не трогаем: в основной базе они свои и более свежие.

Импорт запускается только для базы, которую бот сам создал как временную —
внутри неё есть маркер `sqlite_fallback = 1`. Обычная локальная SQLite
(например, на ноутбуке разработчика) маркера не имеет и в Neon не поедет.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from .config import Settings
from .db import Database

log = logging.getLogger(__name__)

FALLBACK_MARKER_KEY = "sqlite_fallback"
FALLBACK_MARKER_VALUE = "1"


def set_fallback_marker(db: Database) -> None:
    """Помечаем временную базу (вызывается, когда ушли на SQLite-запасной)."""
    try:
        conn = sqlite3.connect(str(db.path))
        try:
            conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (FALLBACK_MARKER_KEY, FALLBACK_MARKER_VALUE),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — маркер не критичен
        log.exception("Не смог пометить временную базу")


def _read_sqlite(path: Path) -> list[dict]:
    """Читаем чаты и сообщения из временной базы (обычным sqlite3)."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        chats = [dict(row) for row in conn.execute("SELECT * FROM chats ORDER BY id")]
        messages = [
            dict(row)
            for row in conn.execute("SELECT * FROM messages ORDER BY id")
        ]
    finally:
        conn.close()
    by_chat: dict[int, list[dict]] = {}
    for message in messages:
        by_chat.setdefault(int(message["chat_id"]), []).append(message)
    for chat in chats:
        chat["messages"] = by_chat.get(int(chat["id"]), [])
    return chats


def _marker_value(path: Path) -> str | None:
    try:
        conn = sqlite3.connect(str(path))
        try:
            row = conn.execute(
                "SELECT value FROM kv WHERE key = ?", (FALLBACK_MARKER_KEY,)
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None


def _set_marker(path: Path, value: str) -> None:
    try:
        conn = sqlite3.connect(str(path))
        try:
            conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (FALLBACK_MARKER_KEY, value),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        log.exception("Не смог обновить маркер временной базы")


def _archive(path: Path) -> Path | None:
    """Переименовываем файл, чтобы его больше не импортировали."""
    target = path.with_name(f"{path.name}.imported-{int(time.time())}")
    try:
        path.rename(target)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(path) + suffix)
            if sidecar.exists():
                try:
                    sidecar.rename(Path(str(target) + suffix))
                except Exception:  # noqa: BLE001
                    pass
        return target
    except Exception:  # noqa: BLE001
        log.exception("Не смог переименовать временную базу %s", path)
        return None


async def import_sqlite_chats(sqlite_path: Path, db: Database) -> int:
    """Копируем чаты и сообщения в основную базу. Возвращает число чатов."""
    chats = [chat for chat in _read_sqlite(sqlite_path) if chat["messages"]]
    if not chats:
        return 0

    imported = 0
    newest_for_user: dict[int, int] = {}
    for chat in chats:
        user_id = int(chat["user_id"])
        created = await db.create_chat(
            user_id,
            provider=chat.get("provider") or "deepseek_web",
            model=chat.get("model") or "",
            title=chat.get("title") or "Новый чат",
            thinking=bool(chat.get("thinking")),
            search=bool(chat.get("search")),
        )
        for message in chat["messages"]:
            content = message.get("content") or ""
            role = message.get("role") or "user"
            await db.add_message(created.id, role, content)
        newest_for_user[user_id] = created.id
        imported += 1

    for user_id, chat_id in newest_for_user.items():
        try:
            await db.set_active_chat(user_id, chat_id)
        except Exception:  # noqa: BLE001
            log.exception("Не смог переключить активный чат на перенесённый")
    return imported


async def maybe_import_sqlite(settings: Settings, db: Database) -> int:
    """Вызывается на старте, когда основная (Postgres) база уже подключилась."""
    if db.url is None:
        return 0
    path = settings.db_path
    if not path.exists():
        return 0
    if _marker_value(path) != FALLBACK_MARKER_VALUE:
        # Это не наша временная база (например, локальная) — не трогаем.
        return 0

    try:
        imported = await import_sqlite_chats(path, db)
    except Exception:  # noqa: BLE001 — импорт не должен мешать запуску
        log.exception("Не удалось перенести чаты из временной базы")
        return 0

    if imported <= 0:
        # Чатов нет — просто убираем временный файл, чтобы не мешал.
        _archive(path)
        return 0

    archived = _archive(path)
    if archived is None:
        # Не смогли переименовать — помечаем, чтобы не импортировать дважды.
        _set_marker(path, "imported")
    log.warning(
        "Перенёс %s чатов из временной SQLite в основную базу%s",
        imported,
        f" (архив: {archived.name})" if archived else "",
    )
    return imported