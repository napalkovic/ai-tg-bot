"""Генерация коротких видео по текстовому описанию — бесплатно, без ключей.

Схема: публичные демо-пространства Hugging Face (Gradio API). Ключ не нужен,
оплата не нужна; у бесплатных Space бывают очереди и лимит на IP — поэтому
есть цепочка запасных пространств.

Порядок:
1. `Lightricks/LTX-Video-distilled` — быстро (десятки секунд), режим
   text-to-video, 512×768, 2–3 секунды.
2. `multimodalart/wan2-1-fast` — другой движок (Wan 2.1 fast), если LTX занят.

Ограничения Telegram: видео отправляем файлом ≤ 45 МБ (лимит Bot API 50 МБ).

Запрос на русском переводим на английский (движки лучше понимают английский).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re

import httpx

from .config import Settings
from .providers.base import ProviderError, RateLimited

log = logging.getLogger(__name__)

LTX_SPACE = "lightricks/ltx-video-distilled"
WAN_SPACE = "multimodalart/wan2-1-fast"
WAN_HEIGHT = 512
WAN_WIDTH = 896
WAN_STEPS = 4
WAN_GUIDANCE = 1.0

NEGATIVE_PROMPT = (
    "worst quality, low quality, blurry, jittery, distorted, static, "
    "watermark, text, logo, deformed"
)

VIDEO_TIMEOUT = 240.0          # на одно пространство: дольше — запасное
DOWNLOAD_TIMEOUT = 180.0
MAX_VIDEO_BYTES = 45 * 1024 * 1024
MIN_VIDEO_BYTES = 8 * 1024     # меньше — это явно не видео
DEFAULT_SECONDS = 2.0

CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)
VIDEO_MAGIC = (b"ftyp", b"\x1aE\xdf\xa3")  # mp4 (ftyp на 4-8 байте) / webm


class VideoError(ProviderError):
    """Не удалось сгенерировать видео."""


def backend_label(settings: Settings) -> str:
    return "LTX-Video (бесплатно)"


def _headers(settings: Settings | None) -> dict[str, str]:
    """Свой UA и, если задан, бесплатный HF-токен (даёт больше квоты GPU)."""
    headers = {"User-Agent": "BobbyBot/1.0 (Telegram assistant)"}
    token = (settings.hf_token if settings is not None else "") or ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _space_host(space_id: str) -> str:
    """«owner/Name.With.Dots» → «owner-name-with-dots.hf.space»."""
    slug = re.sub(r"[^a-z0-9]+", "-", space_id.lower()).strip("-")
    return f"{slug}.hf.space"


async def _to_english(prompt: str) -> str:
    """Перевод на английский (не критично — при ошибке шлём как есть)."""
    if not CYRILLIC.search(prompt):
        return prompt
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            response = await client.get(
                "https://translate.googleapis.com/translate_a/single",
                params={
                    "client": "gtx",
                    "sl": "auto",
                    "tl": "en",
                    "dt": "t",
                    "q": prompt[:800],
                },
            )
            response.raise_for_status()
            data = response.json()
            translated = "".join(
                chunk[0] for chunk in data[0] if chunk and chunk[0]
            ).strip()
            if translated:
                return translated
    except Exception:  # noqa: BLE001
        log.info("Видео: не удалось перевести запрос, отправляю как есть")
    return prompt


async def _call_space(
    space_id: str, endpoint: str, payload: list, timeout: float,
    settings: Settings | None = None,
) -> object:
    """Gradio: POST /call/<endpoint> → event_id, затем SSE до complete/error."""
    host = _space_host(space_id)
    url = f"https://{host}/gradio_api/call/{endpoint}"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=20.0),
            follow_redirects=True,
            headers=_headers(settings),
        ) as client:
            response = await client.post(url, json={"data": payload})
            if response.status_code == 429:
                raise RateLimited(
                    "Бесплатное пространство с видео сейчас перегружено. "
                    "Попробуй через пару минут."
                )
            if response.status_code >= 400:
                raise VideoError(
                    f"Видео-сервис ответил ошибкой {response.status_code}."
                )
            try:
                event_id = response.json().get("event_id")
            except ValueError as exc:
                raise VideoError("Видео-сервис вернул непонятный ответ.") from exc
            if not event_id:
                raise VideoError("Видео-сервис не принял запрос.")

            event: str | None = None
            async with client.stream(
                "GET", f"{url}/{event_id}"
            ) as stream:
                if stream.status_code >= 400:
                    raise VideoError(
                        f"Видео-сервис ответил ошибкой {stream.status_code}."
                    )
                async for line in stream.aiter_lines():
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data = line[5:].strip()
                        if event == "complete":
                            try:
                                return json.loads(data)
                            except json.JSONDecodeError as exc:
                                raise VideoError(
                                    "Видео-сервис прислал битый ответ."
                                ) from exc
                        if event == "error":
                            raise VideoError(
                                "Видео-сервис не смог сгенерировать видео "
                                "(возможно, очередь или лимит бесплатного тарифа)."
                            )
    except httpx.HTTPError as exc:
        raise VideoError(f"Не дозвонился до видео-сервиса: {exc}") from exc
    raise VideoError("Видео-сервис не прислал результат.")


def _find_video_url(payload) -> str | None:
    """Рекурсивно ищем ссылку/путь на mp4/webm в ответе Gradio."""
    if isinstance(payload, dict):
        url = payload.get("url")
        if isinstance(url, str) and (".mp4" in url or ".webm" in url or ".mov" in url):
            return url
        for value in payload.values():
            found = _find_video_url(value)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _find_video_url(item)
            if found:
                return found
    elif isinstance(payload, str) and (".mp4" in payload or ".webm" in payload):
        return payload
    return None


async def _download(url: str, settings: Settings | None = None) -> bytes:
    """Качаем готовый ролик (с повторами: бесплатные файловые серверы капризны)."""
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(DOWNLOAD_TIMEOUT, connect=20.0),
                follow_redirects=True,
                headers=_headers(settings),
            ) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code >= 400:
                        raise VideoError(
                            f"Не удалось скачать готовое видео (HTTP {response.status_code})."
                        )
                    chunks: list[bytes] = []
                    size = 0
                    async for piece in response.aiter_bytes(256 * 1024):
                        size += len(piece)
                        if size > MAX_VIDEO_BYTES:
                            raise VideoError(
                                "Видео получилось больше 45 МБ — Telegram такой не примет."
                            )
                        chunks.append(piece)
            data = b"".join(chunks)
            if len(data) < MIN_VIDEO_BYTES:
                raise VideoError("Видео-сервис прислал пустой файл.")
            return data
        except httpx.HTTPError as exc:
            last_error = exc
            await asyncio.sleep(1 + attempt)
    raise VideoError(f"Не удалось скачать готовое видео: {last_error!r}")


def _clamp_seconds(settings: Settings) -> float:
    try:
        seconds = float(settings.video_seconds)
    except (TypeError, ValueError):
        seconds = DEFAULT_SECONDS
    return max(2.0, min(seconds, 5.0))


async def _generate_ltx(prompt: str, settings: Settings) -> bytes:
    """LTX-Video-distilled: текст → видео (2–3 секунды, 512×768)."""
    payload = [
        prompt[:900],
        NEGATIVE_PROMPT,
        None,          # input_image_filepath — нам не нужен, делаем с нуля
        None,          # input_video_filepath
        512,           # height
        768,           # width
        "text-to-video",
        _clamp_seconds(settings),
        9,             # frames to use
        random.randint(1, 2_000_000_000),
        True,
        1.0,           # guidance
        True,          # improve texture
    ]
    result = await _call_space(
        LTX_SPACE, "text_to_video", payload, VIDEO_TIMEOUT, settings
    )
    url = _find_video_url(result)
    if not url:
        raise VideoError("Видео-сервис не прислал ссылку на ролик.")
    log.info("Видео готово через LTX-Video")
    return await _download(url, settings)


async def _generate_wan(prompt: str, settings: Settings) -> bytes:
    """Wan 2.1 fast — запасной движок, если LTX занят."""
    payload = [
        None,                      # input_image (делаем с нуля)
        prompt[:900],
        WAN_HEIGHT,
        WAN_WIDTH,
        NEGATIVE_PROMPT,
        _clamp_seconds(settings),
        WAN_GUIDANCE,
        WAN_STEPS,
        random.randint(1, 2_000_000_000),
        True,
    ]
    result = await _call_space(
        WAN_SPACE, "generate_video", payload, VIDEO_TIMEOUT, settings
    )
    url = _find_video_url(result)
    if not url:
        raise VideoError("Видео-сервис не прислал ссылку на ролик.")
    log.info("Видео готово через Wan 2.1 fast")
    return await _download(url, settings)


async def generate_video(prompt: str, settings: Settings) -> bytes:
    """Генерируем видео: LTX → Wan 2.1 fast. Возвращает байты mp4/webm."""
    prompt = " ".join((prompt or "").split())
    if not prompt:
        raise VideoError("Пустое описание — напиши, что показать в видео 🙂")
    art_prompt = await _to_english(prompt)

    errors: list[str] = []
    for name, factory in (
        ("LTX", _generate_ltx),
        ("Wan", _generate_wan),
    ):
        try:
            return await factory(art_prompt, settings)
        except ProviderError as exc:
            log.warning("Видео через %s не вышло: %s", name, exc.message)
            errors.append(exc.message)
        except Exception as exc:  # noqa: BLE001
            log.exception("Неожиданная ошибка генерации видео через %s", name)
            errors.append(str(exc)[:200])

    reason = errors[-1] if errors else "сервис недоступен"
    hint = (
        ""
        if settings.hf_token
        else (
            "\nЕсли это повторяется — добавь бесплатный HF_TOKEN "
            "(huggingface.co → Settings → Access Tokens): с ним у бесплатных "
            "видео-пространств больше квота."
        )
    )
    raise VideoError(
        "Не получилось сгенерировать видео. "
        f"Причина: {reason} Попробуй ещё раз через минуту.{hint}"
    )