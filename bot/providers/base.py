"""Общие типы для провайдеров моделей."""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Union


@dataclass
class Delta:
    """Кусочек ответа (для стриминга в Telegram)."""

    text: str


@dataclass
class Done:
    """Ответ завершён. `cid` — идентификатор ветки на стороне провайдера."""

    cid: str | None = None


Event = Union[Delta, Done]


class ProviderError(RuntimeError):
    """Понятная пользователю ошибка провайдера."""

    def __init__(self, message: str, *, retry_after: float | None = None):
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after


class AuthRequired(ProviderError):
    """Сессия DeepSeek протухла / не найдена."""


class RateLimited(ProviderError):
    """Слишком часто / DeepSeek включил защиту."""


ProviderStream = AsyncIterator[Event]
