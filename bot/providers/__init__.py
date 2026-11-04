"""Фабрика провайдеров: по ключу из настроек чата отдаёт готовый объект."""

from __future__ import annotations

import logging

from ..config import DEEPSEEK_WEB, Settings
from .base import ProviderError
from .deepseek_web import DeepSeekWebProvider
from .openai_compat import OpenAICompatProvider

log = logging.getLogger(__name__)


class ProviderRegistry:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._cache: dict[str, object] = {}

    def get(self, key: str):
        if key in self._cache:
            return self._cache[key]

        cfg = self.settings.provider(key)
        if cfg is None:
            raise ProviderError(
                f"Провайдер «{key}» не настроен. Открой /settings и выбери доступный."
            )

        if cfg.kind == DEEPSEEK_WEB or cfg.key == DEEPSEEK_WEB:
            provider = DeepSeekWebProvider(cfg, self.settings)
        else:
            provider = OpenAICompatProvider(cfg, self.settings)

        self._cache[key] = provider
        return provider

    async def aclose(self) -> None:
        for provider in self._cache.values():
            close = getattr(provider, "aclose", None)
            if close is not None:
                await close()
        self._cache.clear()


__all__ = ["ProviderRegistry", "DeepSeekWebProvider", "OpenAICompatProvider"]
