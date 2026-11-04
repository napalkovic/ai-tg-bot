"""Поиск и скачивание музыки (SoundCloud, без ключей и без записи).

Схема для слабого сервера:
- сначала только ЛЁГКИЙ поиск (список кандидатов, ничего не качаем);
- пользователь выбирает трек кнопкой (показываем до 10 вариантов);
- только выбранный трек скачивается (звук) и отправляется в Telegram.

Почему SoundCloud, а не YouTube:
- SoundCloud не даёт серверам «проверку не робот» — YouTube это делает
  постоянно (Render наблюдали 3 ноября 2026);
- треки там в mp3, есть публичный поиск (`scsearch` в yt-dlp), ключи и
  аккаунты не нужны.

Вход в аккаунт SoundCloud (опция): переменная окружения
`SOUNDCLOUD_OAUTH_TOKEN`. Если задана — ВСЕ запросы идут от имени
аккаунта (yt-dlp: username="oauth" + токен), тогда даже коммерческие
треки, которые для гостей лежат в виде 30-секундного отрывка, отдаются
целиком. Токен ввёл владелец 4 ноября 2026.

Ограничения и защита:
- не берём треки длиннее ~15 минут (это обычно подкасты и миксы);
- если файл больше 30 МБ — вообще не качаем (просьба хозяина);
- подстраховка: если файл пришёл 30-секундным (токен просрочен), бот
  не отдаст его и честно скажет об этом;
- если всё сорвалось — человекопонятная причина + ссылка «Слушай тут».
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Лимиты безопасного размера/длины.
MAX_TRACK_SECONDS = 15 * 60
# Если файл больше 30 МБ — не качаем вообще.
MAX_AUDIO_BYTES = 30 * 1024 * 1024

DOWNLOAD_TIMEOUT = 240.0
SEARCH_TIMEOUT = 75.0  # раньше было 120 — из-за «висящего» поиска ответы уходили поздно
DOWNLOAD_ATTEMPTS = 2

# Поиск по SoundCloud через yt-dlp: публичный, без API-ключа.
SEARCH_PREFIX = "scsearch10"

# Необязательный вход в SoundCloud: если на Render задана переменная
# SOUNDCLOUD_OAUTH_TOKEN, скачивание идёт от имени аккаунта — тогда даже
# «сниппетные» (Go+/коммерческие) треки отдаются целиком, а не 30-секундным
# отрывком. Токен — это cookie `oauth_token` со soundcloud.com.
SOUNDCLOUD_OAUTH_TOKEN_ENV = "SOUNDCLOUD_OAUTH_TOKEN"


def _login_kwargs() -> dict:
    """yt-dlp login options: только oauth-токен (username/password не работает)."""
    import os

    token = (os.environ.get(SOUNDCLOUD_OAUTH_TOKEN_ENV) or "").strip()
    if not token:
        return {}
    # В документации SoundcloudIE: username должен быть ровно "oauth",
    # пароль — сам токен.
    return {"username": "oauth", "password": token}


class MusicError(Exception):
    """Понятная человеку ошибка поиска/скачивания музыки."""


@dataclass
class Track:
    title: str
    performer: str
    url: str
    duration: int


def _clean_seconds(value) -> int:
    try:
        seconds = int(value or 0)
    except (TypeError, ValueError):
        seconds = 0
    return max(0, seconds)


def _track_from(item: dict) -> Track | None:
    """Дескриптор трека из результата поиска: не эфир, 1 сек..15 мин, есть URL."""
    if not isinstance(item, dict):
        return None
    if item.get("is_live") or item.get("live_status") in {
        "is_live", "is_upcoming",
    }:
        return None
    seconds = _clean_seconds(item.get("duration"))
    if seconds <= 0 or seconds > MAX_TRACK_SECONDS:
        return None
    url = item.get("webpage_url") or item.get("url") or ""
    if not url and item.get("id"):
        url = f"https://soundcloud.com/{item['id']}"
    if not url:
        return None
    return Track(
        title=(item.get("title") or "Без названия").strip(),
        performer=(
            item.get("uploader") or item.get("channel") or "SoundCloud"
        ).strip(),
        url=url,
        duration=seconds,
    )


def _search_sync(query: str) -> list[dict]:
    import yt_dlp  # локальный импорт: бот не падает, если пакет не поставился

    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extract_flat": "in_playlist",
        # Устойчивость: короткие таймауты и повторные попытки страниц.
        "socket_timeout": 25,
        "extractor_retries": 2,
        "retries": 2,
    }
    options.update(_login_kwargs())
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                data = ydl.extract_info(
                    f"{SEARCH_PREFIX}:{query}", download=False
                )
            if not data:
                return []
            return list(data.get("entries") or [])
        except Exception as exc:  # noqa: BLE001 - yt-dlp бросает всё подряд
            last_error = exc
            log.info("Поиск музыки, попытка %s не удалась: %s", attempt + 1, exc)
    raise last_error if last_error else RuntimeError("Поиск не удался")


async def find_tracks(query: str, *, limit: int = 10) -> list[Track]:
    """Лёгкий поиск: кандидаты по запросу, без скачивания.

    По просьбе хозяина показываем до 10 вариантов, каждый можно скачать
    (полные треки — благодаря входу в аккаунт SoundCloud через токен).
    """
    query = " ".join(query.split())
    if not query:
        raise MusicError("Пустой запрос — напиши название или исполнителя 🙂")

    try:
        results = await asyncio.wait_for(
            asyncio.to_thread(_search_sync, query), timeout=SEARCH_TIMEOUT
        )
    except asyncio.TimeoutError:
        raise MusicError("Поиск занял слишком много времени, попробуй ещё раз.")
    except Exception as exc:  # noqa: BLE001 - yt-dlp бросает всё подряд
        log.warning("Поиск музыки не удался: %s", exc)
        raise MusicError(
            "Не смог прочитать каталог музыки. Попробуй ещё раз через минуту."
        ) from exc

    tracks: list[Track] = []
    seen: set[str] = set()
    for item in results:
        track = _track_from(item)
        if track is None or track.url in seen:
            continue
        tracks.append(track)
        seen.add(track.url)
        if len(tracks) >= max(1, limit):
            break
    if not tracks:
        raise MusicError(
            "Не нашёл подходящий трек 🤷 Уточни название или исполнителя."
        )
    return tracks


def _download_sync(url: str, out_dir: Path) -> Path:
    import yt_dlp

    can_postprocess = shutil.which("ffmpeg") is not None
    if can_postprocess:
        options = {
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        }
    else:
        # Без ffmpeg берём готовый звук, как отдаёт сайт.
        options = {"format": "bestaudio/best"}

    options.update({
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "outtmpl": str(out_dir / "audio.%(ext)s"),
        # Больше 30 МБ не качаем вообще.
        "max_filesize": MAX_AUDIO_BYTES,
        "retries": 2,
    })
    options.update(_login_kwargs())

    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.extract_info(url, download=True)

    files = [
        path for path in out_dir.iterdir()
        if path.is_file() and path.suffix.lower() != ".part"
    ]
    if not files:
        raise RuntimeError("Файл не появился (трек большой или недоступен)")
    # Несколько файлов не должно появиться, но если появилось — берём первый.
    return files[0]


def _ffprobe_seconds(path: Path) -> int:
    """Фактическая длительность файла (0, если ffprobe не установлен)."""
    if shutil.which("ffprobe") is None:
        return 0
    import subprocess

    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "csv=p=0", str(path),
            ],
            capture_output=True, timeout=60,
        )
        return _clean_seconds(float(result.stdout.decode().strip() or 0))
    except Exception:  # noqa: BLE001
        return 0


def _safe_name(title: str) -> str:
    keep = "".join(
        char if char.isalnum() or char in " -_()" else "_"
        for char in title
    )
    cleaned = keep.strip() or "audio"
    return cleaned[:60]


def _human_reason(exc: Exception) -> str:
    """Превращаем сырую ошибку yt-dlp в понятную фразу для чата."""
    text = str(exc)
    low = text.lower()
    if "login" in low or "authentication" in low:
        return "автор открыл этот трек только для зарегистрированных слушателей"
    if "private" in low or "unavailable" in low or "removed" in low:
        return "трек стал недоступен или скрыт автором"
    if ("age" in low) and ("confirm" in low or "restrict" in low):
        return "трек с ограничением по возрасту"
    if ("http" in low) and ("403" in low or "429" in low):
        return "сайт временно ограничил доступ (403/429)"
    if "timed out" in low or "timeout" in low:
        return "сайт не ответил вовремя"
    snippet = " ".join(text.split())[:140]
    return snippet or "неизвестная ошибка"


async def download_track(track: Track) -> tuple[bytes, str, int]:
    """Скачиваем аудио трека, возвращаем (байты, имя файла, длительность)."""
    last_error: Exception | None = None
    for attempt in range(DOWNLOAD_ATTEMPTS):
        with tempfile.TemporaryDirectory(prefix="bobby_music_") as tmp:
            out_dir = Path(tmp)
            try:
                path = await asyncio.wait_for(
                    asyncio.to_thread(_download_sync, track.url, out_dir),
                    timeout=DOWNLOAD_TIMEOUT,
                )
                data = path.read_bytes()
                if not data:
                    raise MusicError("Файл трека пришёл пустым.")
                if len(data) > MAX_AUDIO_BYTES:
                    raise MusicError("Трек больше 30 МБ — не качаю.")

                # Подстраховка: ждали полный трек, а файл — короткий отрывок
                # (так бывает, если токен просрочен или не задан).
                actual = _ffprobe_seconds(path)
                if (
                    track.duration >= 90 and actual and actual <= 40
                ):
                    raise MusicError(
                        "SoundCloud отдал только отрывок этого трека "
                        "(проверь токен аккаунта). Выбери другой вариант."
                    )

                name = f"{_safe_name(track.title)}{path.suffix}"
                return data, name, max(track.duration, actual)
            except asyncio.TimeoutError:
                last_error = RuntimeError("Скачивание не уложилось в таймаут")
            except MusicError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                log.info(
                    "Скачивание не удалось (попытка %s): %s", attempt + 1, exc
                )
    log.warning(
        "Не удалось скачать трек %s: %s(%s)",
        track.url, type(last_error).__name__, last_error,
    )
    reason = _human_reason(last_error or RuntimeError("неизвестная ошибка"))
    raise MusicError(
        f"Не получилось скачать трек ({reason}).\n"
        f"Слушай по ссылке: {track.url}"
    ) from last_error
