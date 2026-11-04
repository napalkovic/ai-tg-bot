"""Хранилище: чаты, сообщения, активный чат пользователя.

Работает с SQLite (локально) и PostgreSQL (на сервере, если задан DATABASE_URL).
Публичный интерфейс один и тот же, поэтому обработчики о разнице не знают.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse, urlunparse

import aiosqlite

SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS chats (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    title       TEXT    NOT NULL,
    provider    TEXT    NOT NULL,
    model       TEXT    NOT NULL DEFAULT '',
    thinking    INTEGER NOT NULL DEFAULT 0,
    search      INTEGER NOT NULL DEFAULT 0,
    ds_cid      TEXT,
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chats_user ON chats(user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role        TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    created_at  REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);

CREATE TABLE IF NOT EXISTS user_state (
    user_id        INTEGER PRIMARY KEY,
    active_chat_id INTEGER
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

SCHEMA_POSTGRES = """
CREATE TABLE IF NOT EXISTS chats (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    title       TEXT   NOT NULL,
    provider    TEXT   NOT NULL,
    model       TEXT   NOT NULL DEFAULT '',
    thinking    INTEGER NOT NULL DEFAULT 0,
    search      INTEGER NOT NULL DEFAULT 0,
    ds_cid      TEXT,
    created_at  DOUBLE PRECISION NOT NULL,
    updated_at  DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chats_user ON chats(user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id          BIGSERIAL PRIMARY KEY,
    chat_id     BIGINT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role        TEXT   NOT NULL,
    content     TEXT   NOT NULL,
    created_at  DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);

CREATE TABLE IF NOT EXISTS user_state (
    user_id        BIGINT PRIMARY KEY,
    active_chat_id BIGINT
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_SYSTEM = "system"


@dataclass
class Chat:
    id: int
    user_id: int
    title: str
    provider: str
    model: str
    thinking: bool
    search: bool
    ds_cid: str | None
    created_at: float
    updated_at: float


def _chat(row: dict[str, Any]) -> Chat:
    return Chat(
        id=row["id"],
        user_id=row["user_id"],
        title=row["title"],
        provider=row["provider"],
        model=row["model"],
        thinking=bool(row["thinking"]),
        search=bool(row["search"]),
        ds_cid=row["ds_cid"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _translate(sql: str) -> str:
    """? -> $1, $2… для asyncpg."""
    out: list[str] = []
    index = 0
    for char in sql:
        if char == "?":
            index += 1
            out.append(f"${index}")
        else:
            out.append(char)
    return "".join(out)


def _pg_dsn(url: str) -> tuple[str, bool, bool]:
    """Чистим DSN от параметров, которые asyncpg не понимает (sslmode и т.п.).

    Возвращает (dsn, использовать_ssl, можно_откатиться_на_без_ssl).
    """
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    sslmode = (query.get("sslmode") or ["prefer"])[0].lower()
    clean = urlunparse(parsed._replace(query=""))
    if sslmode in {"require", "verify-ca", "verify-full"}:
        return clean, True, False
    if sslmode in {"disable", "allow"}:
        return clean, False, False
    # prefer (по умолчанию): сначала пробуем SSL, при отказе — обычное соединение
    return clean, True, True


def _looks_like_connection_error(exc: Exception) -> bool:
    if isinstance(exc, (OSError, ConnectionError)):
        return True
    return "Connection" in type(exc).__name__


class Database:
    def __init__(self, *, path: Path | None = None, url: str | None = None):
        if path is None and url is None:
            raise ValueError("Нужен path (SQLite) или url (Postgres)")
        self.path = path
        self.url = url
        self._conn: aiosqlite.Connection | None = None
        self._pool = None

    @property
    def is_postgres(self) -> bool:
        return self.url is not None

    @property
    def target(self) -> str:
        return "PostgreSQL" if self.is_postgres else str(self.path)

    # --- подключение -----------------------------------------------------

    async def connect(self) -> None:
        if self.url:
            await self._connect_pool()
            assert self._pool is not None
            await self._pool.execute(SCHEMA_POSTGRES)
            return

        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA_SQLITE)
        await self._conn.commit()

    async def _connect_pool(self) -> None:
        assert self.url is not None
        dsn, ssl, ssl_optional = _pg_dsn(self.url)
        options = dict(
            min_size=0,
            max_size=5,
            command_timeout=30,
            # Neon засыпает при простое — не держим соединения вечно
            max_inactive_connection_lifetime=60,
        )
        try:
            self._pool = await self._make_pool(dsn, ssl, options)
        except (OSError, ConnectionError):
            if not (ssl and ssl_optional):
                raise
            self._pool = await self._make_pool(dsn, False, options)

    async def _make_pool(self, dsn: str, ssl: bool, options: dict):
        import asyncpg

        pool = await asyncpg.create_pool(dsn, ssl=ssl, **options)
        try:
            # min_size=0 не подключается сразу — проверяем соединение явно,
            # чтобы успеть откатиться на вариант без SSL.
            async with pool.acquire() as conn:
                await conn.execute("SELECT 1")
        except BaseException:
            await pool.close()
            raise
        return pool

    async def _reset_pool(self) -> None:
        if self._pool is not None:
            try:
                await self._pool.close()
            except Exception:  # noqa: BLE001
                pass
        self._pool = None
        await self._connect_pool()

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # --- низкий уровень ---------------------------------------------------

    async def _fetch(self, sql: str, params: tuple = ()) -> list[dict]:
        return await self._with_retry(self._fetch_once, sql, params)

    async def _execute(self, sql: str, params: tuple = ()) -> None:
        await self._with_retry(self._execute_once, sql, params)

    async def _with_retry(self, operation, sql: str, params: tuple):
        try:
            return await operation(sql, params)
        except Exception as exc:  # noqa: BLE001
            # Neon/облачный Postgres может закрыть соединение при простое.
            if self._pool is None or not _looks_like_connection_error(exc):
                raise
            await self._reset_pool()
            return await operation(sql, params)

    async def _fetch_once(self, sql: str, params: tuple) -> list[dict]:
        if self._pool is not None:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(_translate(sql), *params)
            return [dict(row) for row in rows]

        assert self._conn is not None
        cur = await self._conn.execute(sql, params)
        try:
            rows = await cur.fetchall()
        finally:
            await cur.close()
        return [dict(row) for row in rows]

    async def _fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        rows = await self._fetch(sql, params)
        return rows[0] if rows else None

    async def _execute_once(self, sql: str, params: tuple) -> None:
        if self._pool is not None:
            async with self._pool.acquire() as conn:
                await conn.execute(_translate(sql), *params)
            return

        assert self._conn is not None
        await self._conn.execute(sql, params)
        await self._conn.commit()

    # --- чаты ------------------------------------------------------------

    async def create_chat(
        self,
        user_id: int,
        *,
        provider: str,
        model: str,
        title: str = "Новый чат",
        thinking: bool = False,
        search: bool = False,
    ) -> Chat:
        now = time.time()
        row = await self._fetchone(
            "INSERT INTO chats (user_id, title, provider, model, thinking,"
            " search, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (user_id, title, provider, model, int(thinking), int(search), now, now),
        )
        assert row is not None
        chat = await self.get_chat_by_id(int(row["id"]))
        assert chat is not None
        return chat

    async def get_chat(self, chat_id: int, user_id: int) -> Chat | None:
        row = await self._fetchone(
            "SELECT * FROM chats WHERE id = ? AND user_id = ?", (chat_id, user_id)
        )
        return _chat(row) if row else None

    async def get_chat_by_id(self, chat_id: int) -> Chat | None:
        row = await self._fetchone("SELECT * FROM chats WHERE id = ?", (chat_id,))
        return _chat(row) if row else None

    async def list_chats(self, user_id: int) -> list[Chat]:
        rows = await self._fetch(
            "SELECT * FROM chats WHERE user_id = ? ORDER BY updated_at DESC, id DESC",
            (user_id,),
        )
        return [_chat(row) for row in rows]

    async def update_chat(self, chat_id: int, **fields) -> Chat:
        allowed = {"title", "provider", "model", "thinking", "search", "ds_cid"}
        data = {k: v for k, v in fields.items() if k in allowed}
        if data:
            data["updated_at"] = time.time()
            assignments = ", ".join(f"{k} = ?" for k in data)
            await self._execute(
                f"UPDATE chats SET {assignments} WHERE id = ?",
                (*data.values(), chat_id),
            )
        chat = await self.get_chat_by_id(chat_id)
        assert chat is not None
        return chat

    async def delete_chat(self, chat_id: int) -> None:
        await self._execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        await self._execute("DELETE FROM chats WHERE id = ?", (chat_id,))
        await self._execute(
            "UPDATE user_state SET active_chat_id = NULL WHERE active_chat_id = ?",
            (chat_id,),
        )

    async def delete_all_chats(self, user_id: int) -> int:
        """Удалить все чаты пользователя. Возвращает, сколько удалено."""
        row = await self._fetchone(
            "SELECT COUNT(*) AS n FROM chats WHERE user_id = ?", (user_id,)
        )
        deleted = int(row["n"]) if row else 0
        await self._execute(
            "DELETE FROM messages WHERE chat_id IN"
            " (SELECT id FROM chats WHERE user_id = ?)",
            (user_id,),
        )
        await self._execute("DELETE FROM chats WHERE user_id = ?", (user_id,))
        await self._execute(
            "UPDATE user_state SET active_chat_id = NULL WHERE user_id = ?",
            (user_id,),
        )
        return deleted

    async def clear_chat(self, chat_id: int) -> None:
        """Стереть сообщения и заодно забыть ветку на стороне DeepSeek."""
        await self._execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        await self._execute(
            "UPDATE chats SET ds_cid = NULL, updated_at = ? WHERE id = ?",
            (time.time(), chat_id),
        )

    async def touch_chat(self, chat_id: int, title: str | None = None) -> None:
        if title:
            await self._execute(
                "UPDATE chats SET updated_at = ?, title = ? WHERE id = ?",
                (time.time(), title, chat_id),
            )
        else:
            await self._execute(
                "UPDATE chats SET updated_at = ? WHERE id = ?", (time.time(), chat_id)
            )

    # --- сообщения -------------------------------------------------------

    async def add_message(self, chat_id: int, role: str, content: str) -> None:
        await self._execute(
            "INSERT INTO messages (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, role, content, time.time()),
        )

    async def messages(self, chat_id: int, limit: int | None = None) -> list[dict]:
        if limit is None:
            rows = await self._fetch(
                "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id",
                (chat_id,),
            )
        else:
            rows = await self._fetch(
                "SELECT role, content FROM messages WHERE chat_id = ?"
                " ORDER BY id DESC LIMIT ?",
                (chat_id, limit),
            )
            rows.reverse()
        return [{"role": r["role"], "content": r["content"]} for r in rows]

    async def message_count(self, chat_id: int) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) AS n FROM messages WHERE chat_id = ?", (chat_id,)
        )
        return int(row["n"]) if row else 0

    # --- активный чат ----------------------------------------------------

    async def active_chat(self, user_id: int) -> Chat | None:
        row = await self._fetchone(
            "SELECT active_chat_id FROM user_state WHERE user_id = ?", (user_id,)
        )
        if row and row["active_chat_id"]:
            chat = await self.get_chat(int(row["active_chat_id"]), user_id)
            if chat is not None:
                return chat
        return None

    async def set_active_chat(self, user_id: int, chat_id: int) -> None:
        await self._execute(
            "INSERT INTO user_state (user_id, active_chat_id) VALUES (?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET active_chat_id = excluded.active_chat_id",
            (user_id, chat_id),
        )

    async def delete_user_data(self, user_id: int) -> None:
        chats = await self.list_chats(user_id)
        for chat in chats:
            await self.delete_chat(chat.id)
        await self._execute("DELETE FROM user_state WHERE user_id = ?", (user_id,))

    # --- простое key-value хранилище (токены и т.п.) -----------------------

    async def kv_get(self, key: str) -> str | None:
        rows = await self._fetch(
            "SELECT value FROM kv WHERE key = ?", (key,)
        )
        return rows[0]["value"] if rows else None

    async def kv_set(self, key: str, value: str) -> None:
        await self._execute(
            "INSERT INTO kv (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    async def kv_delete(self, key: str) -> None:
        await self._execute("DELETE FROM kv WHERE key = ?", (key,))

    async def kv_prefix(self, prefix: str) -> dict[str, str]:
        """Все пары key-value с префиксом (для per-user флажков оповещений)."""
        rows = await self._fetch(
            "SELECT key, value FROM kv WHERE key LIKE ?", (prefix + "%",)
        )
        return {row["key"]: row["value"] for row in rows}


def build_history(
    messages: Iterable[dict],
    *,
    max_messages: int,
    max_chars: int,
) -> list[dict]:
    """Берём хвост диалога, укладываясь в лимиты по количеству и по символам."""
    items = list(messages)
    selected: list[dict] = []
    total = 0
    for message in reversed(items):
        length = len(message["content"])
        if selected and (len(selected) >= max_messages or total + length > max_chars):
            break
        selected.append(message)
        total += length
    selected.reverse()
    return selected
