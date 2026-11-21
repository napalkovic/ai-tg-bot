"""Любой OpenAI-совместимый провайдер: OpenRouter, Groq, DeepSeek API."""

from __future__ import annotations

import logging
from typing import AsyncIterator

import openai
from openai import AsyncOpenAI

from ..config import ProviderConfig, Settings
from .base import AuthRequired, Delta, Done, Event, ProviderError, RateLimited

log = logging.getLogger(__name__)

# У OpenRouter бесплатные модели периодически «переезжают» в платные и
# начинают отвечать 404. Держим список запасных бесплатных вариантов:
# если запрошенная модель недоступна, пробуем следующую.
FREE_CHAINS: dict[str, tuple[str, ...]] = {
    "openrouter": (
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "google/gemma-4-31b-it:free",
        "thinkingmachines/inkling:free",
        "nvidia/nemotron-3.5-lightning:free",
        "openrouter/free",
    ),
}

# Запасные модели для картинок — обязательно «зрячие».
VISION_CHAINS: dict[str, tuple[str, ...]] = {
    "openrouter": (
        "nvidia/nemotron-3.5-super-vl:free",
        "google/gemma-4-31b-it:free",
    ),
}


def model_candidates(provider_key: str, requested: str, *, vision: bool = False) -> list[str]:
    """Запрошенная модель первой, дальше — запасные (без повторов)."""
    chain = (VISION_CHAINS if vision else FREE_CHAINS).get(provider_key, ())
    result: list[str] = []
    for name in (requested, *chain):
        if name and name not in result:
            result.append(name)
    return result


def _is_model_error(exc: Exception) -> bool:
    text = str(getattr(exc, "message", "") or exc).lower()
    return "model" in text and any(
        marker in text
        for marker in (
            "not found",
            "unavailable",
            "invalid",
            "does not exist",
            "no endpoints",
            "unknown",
        )
    )


def _map_error(cfg: ProviderConfig, exc: Exception) -> ProviderError:
    if isinstance(exc, openai.AuthenticationError):
        return AuthRequired(
            f"{cfg.label}: ключ API не принят. Проверь {cfg.key.upper()}_API_KEY в .env."
        )
    if isinstance(exc, openai.RateLimitError):
        return RateLimited(
            f"{cfg.label}: лимит бесплатного тарифа исчерпан. Попробуй позже."
        )
    if isinstance(exc, openai.APIConnectionError):
        return ProviderError(
            f"{cfg.label}: нет связи с сервером модели ({exc.__class__.__name__})."
        )
    if isinstance(exc, openai.APIStatusError):
        text = (getattr(exc, "message", "") or str(exc))[:300]
        return ProviderError(f"{cfg.label}: ошибка API {exc.status_code}. {text}")
    return ProviderError(f"{cfg.label}: {str(exc)[:200]}")


class OpenAICompatProvider:
    def __init__(self, cfg: ProviderConfig, settings: Settings):
        self.cfg = cfg
        self.settings = settings
        self.key = cfg.key
        self._client = AsyncOpenAI(
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            timeout=settings.request_timeout,
            max_retries=1,
        )

    async def stream(
        self,
        messages: list[dict],
        *,
        cid: str | None = None,
        model: str = "",
        thinking: bool = False,
        search: bool = False,
    ) -> AsyncIterator[Event]:
        payload = list(messages)
        if self.settings.system_prompt and not any(m["role"] == "system" for m in payload):
            payload.insert(0, {"role": "system", "content": self.settings.system_prompt})

        has_images = any(isinstance(m.get("content"), list) for m in payload)
        candidates = model_candidates(
            self.cfg.key, model or self.cfg.model, vision=has_images
        )
        last_error: ProviderError | None = None

        for index, candidate in enumerate(candidates):
            has_more = index + 1 < len(candidates)
            try:
                stream = await self._client.chat.completions.create(
                    model=candidate,
                    messages=payload,  # type: ignore[arg-type]
                    stream=True,
                )
            except (openai.NotFoundError, openai.BadRequestError) as exc:
                if has_more and _is_model_error(exc):
                    log.warning(
                        "%s: модель %s недоступна (%s) — пробую %s",
                        self.cfg.label, candidate, exc.status_code, candidates[index + 1],
                    )
                    last_error = _map_error(self.cfg, exc)
                    continue
                raise _map_error(self.cfg, exc) from exc
            except openai.RateLimitError as exc:
                # У бесплатных моделей бывают отдельные лимиты на каждую.
                if has_more:
                    log.warning(
                        "%s: лимит на %s — пробую %s",
                        self.cfg.label, candidate, candidates[index + 1],
                    )
                    last_error = _map_error(self.cfg, exc)
                    continue
                raise _map_error(self.cfg, exc) from exc
            except openai.APIStatusError as exc:
                if has_more and exc.status_code >= 500:
                    last_error = _map_error(self.cfg, exc)
                    continue
                raise _map_error(self.cfg, exc) from exc
            except openai.APIError as exc:
                if has_more:
                    last_error = _map_error(self.cfg, exc)
                    continue
                raise _map_error(self.cfg, exc) from exc

            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                text = getattr(delta, "content", None)
                if text:
                    yield Delta(text)
            yield Done()
            return

        raise last_error or ProviderError(f"{self.cfg.label}: не удалось получить ответ.")

    async def aclose(self) -> None:
        await self._client.close()
