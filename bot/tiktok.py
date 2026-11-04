"""ТикТок: ссылка → видео без водяного знака.

Правило простое: yt-dlp умеет поддерживать свой tiktok-экстрактор — он
отдаёт оригинальный mp4 (тот файл, что сам сервис хранит у себя до
обмазки «водяным знаком»). Никаких сторонних сайтов-даунлоадеров.

Лimits:
- видео ≤ 10 минут;
- файлы больше 45 МБ не отправляем (лимит Telegram Bot API — 50 МБ).
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

MAX_VIDEO_SECONDS = 10 * 60
MAX_VIDEO_BYTES = 45 * 1024 * 1024

DOWNLOAD_TIMEOUT = 180.0

# Любые формы ссылок: обычная, короткие vm./vt., странички t. (ок).
TIKTOK_RE = re.compile(
    r"http[s]?://(?:www\.|vm\.|vt\.|t\.)?tiktok\.com/\S+", re.I
)


class TikTokError(Exception):
    """Понятная человеку ошибка скачивания ТикТока."""


def video_in_url(text: str | None) -> str | None:
    """Первая найденная ссылка на ТикТок в сообщении или None."""
    if not text:
        return None
    match = TIKTOK_RE.search(text)
    return match.group(0) if match else None


def _download_sync(url: str, out_dir: Path):
    import yt_dlp

    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "outtmpl": str(out_dir / "video.%(ext)s"),
        "format": "best/bv*+ba/b",
        "max_filesize": MAX_VIDEO_BYTES,
        "retries": 2,
        "socket_timeout": 25,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)

    files = [
        path for path in out_dir.iterdir()
        if path.is_file() and path.suffix.lower() != ".part"
    ]
    if not files:
        raise RuntimeError("Файл видео не появился (недоступен или слишком большой)")
    title = (info.get("title") if isinstance(info, dict) else "") or "ТикТок"
    uploader = (info.get("uploader") if isinstance(info, dict) else "") or ""
    duration = int(float((info.get("duration") or 0))) if isinstance(info, dict) else 0
    return files[0], title, uploader, duration


def _safe_name(title: str) -> str:
    keep = "".join(
        char if char.isalnum() or char in " -_()" else "_"
        for char in title
    )
    cleaned = keep.strip() or "video"
    return cleaned[:60]


async def download_video(url: str) -> tuple[bytes, str, str]:
    """Скачиваем видео, возвращаем (байты, имя файла, подпись)."""
    url = url.strip()
    if not url:
        raise TikTokError("Пустая ссылка — пришли ссылку на видео 🙂")

    import yt_dlp  # чтобы без скачивания понять: видео ли это вообще

    last_error: Exception | None = None
    for attempt in range(2):
        with tempfile.TemporaryDirectory(prefix="bobby_tiktok_") as tmp:
            out_dir = Path(tmp)
            try:
                path, title, uploader, duration = await asyncio.wait_for(
                    asyncio.to_thread(_download_sync, url, out_dir),
                    timeout=DOWNLOAD_TIMEOUT,
                )
                data = path.read_bytes()
                if not data:
                    raise TikTokError("Файл видео пришёл пустым.")
                if len(data) > MAX_VIDEO_BYTES:
                    raise TikTokError(
                        "Видео больше 45 МБ — Telegram такой не принимает."
                    )
                if duration > MAX_VIDEO_SECONDS:
                    raise TikTokError("Видео длиннее 10 минут — не буду качать.")
                name = f"{_safe_name(title)}{path.suffix}"
                caption = f"🎬 {title}"
                if uploader:
                    caption = f"🎬 {uploader}: {title}"
                return data, name, caption[:1000]
            except asyncio.TimeoutError:
                last_error = RuntimeError("Скачивание не уложилось в таймаут")
            except TikTokError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                log.info(
                    "ТикТок, попытка %s не удалась: %s", attempt + 1, exc
                )
    log.warning(
        "Не удалось скачать видео %s: %s(%s)",
        url, type(last_error).__name__, last_error,
    )
    raise TikTokError(_human_reason(last_error or RuntimeError("неизвестная ошибка")))


def _human_reason(exc: Exception) -> str:
    """Превращаем сырые ошибки yt-dlp в понятные фразы для чата."""
    text = str(exc)
    low = text.lower()
    if "unsupported url" in low or "no video" in low:
        return "это не ссылка на видео ТикТока, пришли ссылку из приложения 🎬"
    if "private" in low or "unavailable" in low or "removed" in low:
        return "видео стало недоступно или скрыто автором"
    if "login" in low or "authentication" in low:
        return "автор закрыл видео для незарегистрированных"
    if "403" in low or "429" in low:
        return "ТикТок временно ограничил доступ (403/429)"
    if "timed out" in low or "timeout" in low:
        return "ТикТок не ответил вовремя"
    snippet = " ".join(text.split())[:140]
    return snippet or "не удалось скачать видео"
