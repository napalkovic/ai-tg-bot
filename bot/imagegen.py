"""Генерация картинок по текстовому запросу.

Основной путь — Cloudflare Workers AI, модель FLUX.1 schnell. У Cloudflare
бесплатный тариф без карты: 10 000 «нейронов» в день, а одна картинка стоит
несколько нейронов — на практике это сотни картинок в день.

Запасной путь — Pollinations (без ключа). У него бесплатный лимит примерно
1 картинка в 45 секунд на IP; если добавить POLLINATIONS_TOKEN, лимита нет.

Запросы на русском переводим на английский: FLUX лучше понимает английский.
Если перевод не удался, рисуем по исходному тексту.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import random
import re
import urllib.parse

import httpx

from .config import Settings
from .providers.base import ProviderError, RateLimited

log = logging.getLogger(__name__)

CLOUDFLARE_API = "https://api.cloudflare.com/client/v4/accounts"
POLLINATIONS = "https://image.pollinations.ai/prompt"
TRANSLATE_API = "https://translate.googleapis.com/translate_a/single"

# Бесплатные модели без регистрации и ключей: публичные demo-пространства
# Hugging Face (Gradio API). FLUX.1-dev даёт заметно более живые картинки,
# чем «schnell»/Pollinations, и при этом бесплатен.
HF_SPACE = "https://multimodalart-flux-1-merged.hf.space"
HF_DEV_SPACE = "black-forest-labs/FLUX.1-dev"
HF_SD35_SPACE = "stabilityai/stable-diffusion-3.5-large"
HF_TIMEOUT = 150.0        # на одно пространство: дольше — переходим к запасному
HF_STEPS = 24            # больше шагов — лучше детали (для FLUX.1-dev/SD3.5)
HF_DEV_STEPS = 24
HF_GUIDANCE = 3.5
HF_SD35_GUIDANCE = 4.5
HF_NEGATIVE = "blurry, low quality, deformed, watermark, text, extra fingers, bad anatomy"

IMAGE_TIMEOUT = 150.0
IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"RIFF")
CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)
# Pollinations отдаёт 402, если запрос похож на браузерный или пришёл без UA.
# Свой честный UA проходит.
USER_AGENT = "BobbyBot/1.0 (Telegram assistant)"


class ImageError(ProviderError):
    """Не удалось нарисовать картинку."""


def _looks_like_image(data: bytes) -> bool:
    return len(data) > 1024 and data.startswith(IMAGE_MAGIC)


def backend_label(settings: Settings) -> str | None:
    """Что рисует картинки — для экрана настроек."""
    if settings.cloudflare_ready():
        return "Cloudflare (FLUX)"
    if settings.image_free_fallback:
        return "FLUX.1-dev (бесплатно)"
    return None


async def _to_english(client: httpx.AsyncClient, prompt: str) -> str:
    """Переводим русский запрос на английский. Ошибки не критичны."""
    if not CYRILLIC.search(prompt):
        return prompt
    try:
        response = await client.get(
            TRANSLATE_API,
            params={
                "client": "gtx",
                "sl": "auto",
                "tl": "en",
                "dt": "t",
                "q": prompt[:1500],
            },
        )
        response.raise_for_status()
        data = response.json()
        parts = [chunk[0] for chunk in data[0] if chunk and chunk[0]]
        translated = "".join(parts).strip()
        if translated:
            log.info("Запрос для картинки переведён: %s", translated[:120])
            return translated
    except Exception:  # noqa: BLE001
        log.info("Не удалось перевести запрос для картинки, рисую как есть")
    return prompt


async def _cloudflare(prompt: str, settings: Settings) -> bytes:
    url = (
        f"{CLOUDFLARE_API}/{settings.cloudflare_account_id}/ai/run/"
        f"{settings.image_model}"
    )
    headers = {"Authorization": f"Bearer {settings.cloudflare_api_token}"}
    body = {
        "prompt": prompt[:2048],
        "steps": 4,
        "seed": random.randint(1, 2_000_000_000),
    }
    try:
        async with httpx.AsyncClient(timeout=IMAGE_TIMEOUT) as client:
            response = await client.post(url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise ImageError(f"Не дозвонился до Cloudflare: {exc}") from exc

    if response.status_code in {401, 403}:
        raise ImageError(
            "Cloudflare не принял ключ. Создай токен через кнопку "
            "«Create a Workers AI API Token» на странице Workers AI → REST API "
            "(или дай права Workers AI: Read и Edit) и обнови CLOUDFLARE_API_TOKEN."
        )
    if response.status_code == 429:
        raise RateLimited(
            "Дневной бесплатный лимит Cloudflare закончился. "
            "Он обновится через несколько часов."
        )
    if response.status_code >= 400:
        raise ImageError(
            f"Cloudflare ответил ошибкой {response.status_code}: "
            f"{response.text[:160]}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise ImageError("Cloudflare вернул непонятный ответ.") from exc

    if not payload.get("success", True):
        errors = payload.get("errors") or []
        detail = errors[0].get("message") if errors else "неизвестная ошибка"
        raise ImageError(f"Cloudflare не нарисовал: {detail}")

    result = payload.get("result") or {}
    encoded = result.get("image") if isinstance(result, dict) else None
    if not encoded:
        raise ImageError("Cloudflare не прислал картинку.")
    try:
        data = base64.b64decode(encoded)
    except Exception as exc:  # noqa: BLE001
        raise ImageError("Cloudflare прислал битую картинку.") from exc
    if not _looks_like_image(data):
        raise ImageError("Cloudflare вернул что-то не похожее на картинку.")

    log.info("Картинка нарисована через Cloudflare FLUX (%s КБ)", len(data) // 1024)
    return data


def _hf_headers(settings: Settings | None) -> dict[str, str]:
    """Заголовки для HF: свой UA и, если задан, бесплатный токен аккаунта.

    Токен не обязателен, но с ним у бесплатных GPU-пространств больше квота
    (без него анонимный лимит на IP быстро кончается).
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    token = (settings.hf_token if settings is not None else "") or ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _space_host(space: str) -> str:
    """«owner/Name.With.Dots» → «https://owner-name-with-dots.hf.space»."""
    slug = re.sub(r"[^a-z0-9]+", "-", space.lower()).strip("-")
    return f"https://{slug}.hf.space"


def _find_file_url(payload) -> str | None:
    """Рекурсивно ищем ссылку на файл картинки в ответе Gradio."""
    if isinstance(payload, dict):
        url = payload.get("url")
        if isinstance(url, str) and re.search(
            r"(?i)\.(png|jpe?g|webp|gif)(\?|$)|/file=", url
        ):
            return url
        for value in payload.values():
            found = _find_file_url(value)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _find_file_url(item)
            if found:
                return found
    elif isinstance(payload, str) and re.search(r"(?i)\.(png|jpe?g|webp|gif)$", payload):
        return payload
    return None


async def _hf_infer(space: str, data: list, settings: Settings) -> str:
    """Gradio: POST /call/infer → event_id, затем SSE до complete/error.

    Возвращает ссылку на готовый файл. Ключ не нужен (но с HF_TOKEN лучше).
    """
    base = _space_host(space)
    timeout = httpx.Timeout(HF_TIMEOUT, connect=20.0)
    headers = _hf_headers(settings)
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, headers=headers
        ) as client:
            response = await client.post(
                f"{base}/gradio_api/call/infer", json={"data": data}
            )
            if response.status_code in {401, 403}:
                raise ImageError(
                    "HF-сервис не принял токен HF_TOKEN — проверь его в настройках."
                )
            if response.status_code == 429:
                raise RateLimited(
                    "Бесплатное пространство с картинками перегружено — "
                    "попробуй через минуту."
                )
            if response.status_code >= 400:
                raise ImageError(
                    f"HF-сервис ответил ошибкой {response.status_code}."
                )
            try:
                event_id = response.json().get("event_id")
            except ValueError as exc:
                raise ImageError("HF-сервис вернул непонятный ответ.") from exc
            if not event_id:
                raise ImageError("HF-сервис не принял запрос.")

            url = None
            event = None
            async with client.stream(
                "GET", f"{base}/gradio_api/call/infer/{event_id}"
            ) as stream:
                if stream.status_code >= 400:
                    raise ImageError(
                        f"HF-сервис ответил ошибкой {stream.status_code}."
                    )
                async for line in stream.aiter_lines():
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        if event == "complete":
                            try:
                                url = _find_file_url(json.loads(line[5:].strip()))
                            except ValueError as exc:
                                raise ImageError(
                                    "HF-сервис прислал битый ответ."
                                ) from exc
                            break
                        if event == "error":
                            raise ImageError(
                                "HF-сервис не смог нарисовать картинку "
                                "(возможно, очередь или лимит бесплатного тарифа)."
                            )
    except ImageError:
        raise
    except httpx.HTTPError as exc:
        raise ImageError(f"Не дозвонился до HF-сервиса: {exc}") from exc

    if not url:
        raise ImageError("HF-сервис не прислал ссылку на картинку.")
    return url


def _hf_payload(space: str, prompt: str, settings: Settings) -> list:
    """Параметры под конкретное пространство (у SD 3.5 есть negative prompt)."""
    seed = random.randint(1, 2_000_000_000)
    steps = settings.image_hf_steps or HF_STEPS
    if "stable-diffusion-3" in space.lower():
        return [
            prompt[:1500],
            HF_NEGATIVE,
            seed,
            True,
            1024,
            1024,
            HF_SD35_GUIDANCE,
            steps,
        ]
    return [
        prompt[:1500],
        seed,
        True,
        1024,
        1024,
        HF_GUIDANCE,
        steps,
    ]


async def _hf_flux_on(prompt: str, settings: Settings, space: str) -> bytes:
    """FLUX.1-dev / SD 3.5 / FLUX-schnell через бесплатное demo-пространство."""
    url = await _hf_infer(
        space, _hf_payload(space, prompt, settings), settings
    )
    data = await _download_ranges(url, settings)
    log.info("Картинка нарисована через %s (%s КБ)", space, len(data) // 1024)
    return data


async def _hf_flux(prompt: str, settings: Settings) -> bytes:
    """Основной бесплатный путь: FLUX.1-dev → SD 3.5 → старое FLUX-демо."""
    spaces: list[str] = []
    for space in (
        settings.image_hf_space or HF_DEV_SPACE,
        settings.image_hf_space_fallback or HF_SD35_SPACE,
        "multimodalart/flux-1-merged",
    ):
        if space and space not in spaces:
            spaces.append(space)

    errors: list[str] = []
    last_error: ProviderError | None = None
    for space in spaces:
        try:
            return await _hf_flux_on(prompt, settings, space)
        except ProviderError as exc:
            log.warning("HF-пространство %s не нарисовало: %s", space, exc.message)
            errors.append(exc.message)
            last_error = exc
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка HF-пространства %s", space)
            errors.append(str(exc)[:200])
    if last_error is not None:
        raise last_error
    raise ImageError(errors[-1] if errors else "бесплатные HF-сервисы недоступны")


async def _download_ranges(url: str, settings: Settings | None = None) -> bytes:
    """Качаем файл кусками по 16 КБ.

    Бесплатный файловый сервер Hugging Face отдаёт максимум ~16 КБ за ответ,
    а на последнем куске вообще не закрывает соединение (присылает все байты
    и ждёт ещё один, которого нет). Поэтому читаем поток и обрываем его сами,
    как только набрали нужное количество байт.
    """
    chunk_size = 16384
    max_size = 12 * 1024 * 1024
    start = 0
    total: int | None = None
    chunks: list[bytes] = []
    done = False

    while not done and start < max_size:
        expected = chunk_size if total is None else min(chunk_size, total - start)
        if expected <= 0:
            break
        part = b""
        last_error: Exception | None = None

        for attempt in range(4):
            got = bytearray()
            try:
                # Новый клиент на каждый кусок: старое соединение иногда
                # «зависает» и httpx ждёт данных до таймаута.
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(20.0, connect=10.0),
                    follow_redirects=True,
                    headers=_hf_headers(settings),
                ) as client:
                    async with client.stream(
                        "GET",
                        url,
                        headers={
                            "Range": f"bytes={start}-{start + chunk_size - 1}",
                            "Connection": "close",
                        },
                    ) as response:
                        if response.status_code == 200:
                            # Сервер проигнорировал Range и отдал файл целиком.
                            return await response.aread()
                        if response.status_code == 416:
                            # Запросили байты за концом файла — уже всё скачали.
                            total = start
                            done = True
                            break
                        if response.status_code != 206:
                            raise ImageError(
                                "Не удалось скачать картинку "
                                f"(HTTP {response.status_code})."
                            )
                        match = re.search(
                            r"/(\d+)$", response.headers.get("content-range", "")
                        )
                        if match:
                            total = int(match.group(1))
                            expected = min(chunk_size, total - start)
                        async for piece in response.aiter_bytes():
                            got += piece
                            if len(got) >= expected:
                                break
                part = bytes(got[:expected])
                break
            except httpx.HTTPError as exc:
                last_error = exc
                if got:
                    # Что-то уже пришло — забираем и продолжим с этого места.
                    part = bytes(got[:expected])
                    break
                if attempt < 3:
                    await asyncio.sleep(1 + attempt)

        if done:
            break
        if not part:
            if total is not None and start >= total:
                break
            raise ImageError(f"Не удалось скачать картинку: {last_error!r}")

        chunks.append(part)
        start += len(part)
        if total is not None:
            if start >= total:
                break
        elif len(part) < expected:
            # Размер файла неизвестен, а сервер отдал меньше запрошенного —
            # это конец.
            break

    if start >= max_size:
        raise ImageError("Картинка получилась слишком большая.")

    data = b"".join(chunks)
    if not _looks_like_image(data):
        raise ImageError("FLUX-сервис вернул что-то не похожее на картинку.")
    return data


def _to_sendable(data: bytes) -> bytes:
    """Telegram надёжнее принимает JPEG/PNG. WebP из FLUX конвертируем."""
    if data.startswith((b"\xff\xd8\xff", b"\x89PNG")):
        return data
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            output = io.BytesIO()
            image.save(output, "JPEG", quality=92, optimize=True)
            return output.getvalue()
    except Exception:  # noqa: BLE001 - без Pillow отдадим как есть
        log.info("Не удалось конвертировать картинку в JPEG, отправляю как есть")
        return data


async def _pollinations(prompt: str, settings: Settings) -> bytes:
    url = f"{POLLINATIONS}/{urllib.parse.quote(prompt[:1500])}"
    params = {
        "width": "768",
        "height": "768",
        "nologo": "true",
        "enhance": "true",
        "seed": str(random.randint(1, 2_000_000_000)),
    }
    headers = {"User-Agent": USER_AGENT}
    if settings.pollinations_token:
        headers["Authorization"] = f"Bearer {settings.pollinations_token}"

    try:
        async with httpx.AsyncClient(
            timeout=IMAGE_TIMEOUT, follow_redirects=True, headers=headers
        ) as client:
            response = await client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise ImageError(f"Не дозвонился до генератора картинок: {exc}") from exc

    if response.status_code in {402, 429}:
        retry = _retry_after(response)
        hint = (
            f" Освободится через {retry} с."
            if retry
            else " Попробуй через минуту."
        )
        raise RateLimited(
            "Бесплатный генератор сейчас занят: лимит — 1 картинка в 45 секунд."
            + hint
        )
    if response.status_code >= 400:
        raise ImageError(
            f"Генератор картинок ответил ошибкой {response.status_code}."
        )

    data = response.content
    if not _looks_like_image(data):
        raise ImageError("Генератор вернул что-то не похожее на картинку.")
    log.info("Картинка нарисована через Pollinations (%s КБ)", len(data) // 1024)
    return data


def _retry_after(response: httpx.Response) -> int | None:
    try:
        error = response.json().get("error") or {}
        match = re.search(r"(\d+)\s*s", str(error.get("message", "")))
        if match:
            return int(match.group(1))
    except Exception:  # noqa: BLE001
        pass
    return None


async def generate_image(prompt: str, settings: Settings) -> bytes:
    """Рисует картинку: Cloudflare → FLUX.1-dev → SD 3.5 → Pollinations.

    Бесплатные HF-пространства перебираются внутри `_hf_flux` (FLUX.1-dev,
    затем SD 3.5, затем старое FLUX-демо); Pollinations — совсем последний
    запасной вариант.
    """
    errors: list[str] = []
    last_error: ProviderError | None = None
    async with httpx.AsyncClient(timeout=IMAGE_TIMEOUT, follow_redirects=True) as client:
        art_prompt = await _to_english(client, prompt)

    if settings.cloudflare_ready():
        try:
            return _to_sendable(await _cloudflare(art_prompt, settings))
        except ProviderError as exc:
            log.warning("Cloudflare не нарисовал: %s", exc.message)
            errors.append(exc.message)
            last_error = exc
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка Cloudflare при генерации картинки")
            errors.append(str(exc)[:200])

    if settings.image_free_fallback:
        try:
            return _to_sendable(await _hf_flux(art_prompt, settings))
        except ProviderError as exc:
            log.warning("Бесплатный FLUX не нарисовал: %s", exc.message)
            errors.append(exc.message)
            last_error = exc
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка бесплатного FLUX")
            errors.append(str(exc)[:200])

        try:
            return _to_sendable(await _pollinations(art_prompt, settings))
        except ProviderError as exc:
            log.warning("Pollinations не нарисовал: %s", exc.message)
            errors.append(exc.message)
            last_error = exc
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка бесплатного генератора картинок")
            errors.append(str(exc)[:200])

    if not settings.cloudflare_ready() and not settings.image_free_fallback:
        raise ImageError(
            "Генерация картинок не настроена. Включи IMAGE_FREE_FALLBACK или "
            "добавь CLOUDFLARE_ACCOUNT_ID и CLOUDFLARE_API_TOKEN — инструкция в README."
        )

    if last_error is not None:
        raise last_error
    reason = errors[-1] if errors else "генератор не настроен"
    raise ImageError(f"Не получилось нарисовать картинку: {reason}")
