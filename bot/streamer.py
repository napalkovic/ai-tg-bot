"""Постепенный вывод ответа в Telegram: редактируем сообщение по мере генерации."""

from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from .formatting import SAFE_LIMIT, md_to_html

log = logging.getLogger(__name__)

THINKING = "⏳ Думаю…"


class Streamer:
    """Пишет ответ в сообщение, аккуратно редактируя его по мере генерации.

    Длинные ответы продолжаются в следующих сообщениях. Разметка применяется
    на каждом шаге; если Telegram её не принял — откатываемся на чистый текст.
    """

    def __init__(self, bot: Bot, chat_id: int, message_id: int, *, interval: float = 1.2):
        self.bot = bot
        self.chat_id = chat_id
        self.message_id = message_id
        self.interval = interval
        self.text = ""
        self._committed = 0
        self._shown_raw: str | None = None
        self._last_edit = 0.0

    # --- публичное -------------------------------------------------------

    async def push(self, chunk: str) -> None:
        self.text += chunk
        if time.monotonic() - self._last_edit >= self.interval:
            await self._render()

    async def flush(self) -> None:
        await self._render()

    async def finish(self) -> None:
        while True:
            rest = self.text[self._committed:]
            if not _needs_split(rest):
                break
            cut = _fit_cut(rest, min(SAFE_LIMIT, len(rest)))
            await self._edit_html(rest[:cut])
            self._committed += cut
            await self._new_message()
        await self._edit_html(self.text[self._committed:])

    async def delete_placeholder(self) -> None:
        try:
            await self.bot.delete_message(self.chat_id, self.message_id)
        except Exception:
            pass

    # --- внутреннее ------------------------------------------------------

    async def _render(self) -> None:
        rest = self.text[self._committed:]
        if not rest or rest == self._shown_raw:
            return
        if _needs_split(rest):
            cut = _fit_cut(rest, min(SAFE_LIMIT, len(rest)))
            await self._edit_html(rest[:cut])
            self._committed += cut
            await self._new_message()
            return
        await self._edit_html(rest)

    async def _new_message(self) -> None:
        message = await self.bot.send_message(self.chat_id, THINKING)
        self.message_id = message.message_id
        self._shown_raw = None
        self._last_edit = 0.0

    async def _edit_html(self, raw: str) -> None:
        displayed = md_to_html(raw) if raw.strip() else "…"
        try:
            await self._edit(displayed, parse_mode="HTML", raw=raw)
        except TelegramBadRequest as exc:
            if "not found" in str(exc):
                await self._new_message()
                await self._edit(displayed, parse_mode="HTML", raw=raw)
                return
            log.debug("HTML не принят (%s) — отправляю текстом", exc)
            await self._edit(raw or "…", parse_mode=None, raw=raw)

    async def _edit(self, text: str, *, parse_mode: str | None, raw: str) -> None:
        try:
            await self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.message_id,
                text=text,
                parse_mode=parse_mode,
                disable_web_page_preview=True,
            )
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
            await self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.message_id,
                text=text,
                parse_mode=parse_mode,
                disable_web_page_preview=True,
            )
        except TelegramBadRequest as exc:
            message = str(exc)
            if "message is not modified" in message:
                self._shown_raw = raw
                return
            raise
        self._shown_raw = raw
        self._last_edit = time.monotonic()


def _split_point(text: str, limit: int) -> int:
    cut = text.rfind("\n", 0, limit)
    if cut < limit // 2:
        cut = text.rfind(" ", 0, limit)
    if cut < limit // 2:
        cut = limit
    return cut


def _fit_cut(text: str, limit: int) -> int:
    """Подбираем границу так, чтобы HTML после конвертации тоже влез в лимит."""
    cut = _split_point(text, max(1, min(limit, len(text))))
    while cut > 1 and len(md_to_html(text[:cut])) > 4000:
        cut = _split_point(text, max(1, int(cut * 0.9)))
    return cut


def _needs_split(text: str) -> bool:
    return len(text) > SAFE_LIMIT or len(md_to_html(text)) > 4000
