"""YouTube: ссылка → понятное описание видео (для ИИ и памяти чата).

Владелец просил (4 ноября 2026): пришли ссылку на YouTube или Shorts — бот
сам «смотрит» видео и запоминает его в текущем чате, как это уже сделано с
фото.

Что делаем без скачивания самого ролика (бережёт сервер):
- yt-dlp вытаскивает название, автора, длительность, описание, главы;
- берём субтитры (русские/английские, авто тоже) и превращаем в текст —
  по ним ИИ понимает, о чём видео;
- если yt-dlp не смог (серверные IP YouTube иногда режет), остаётся
  oEmbed — хотя бы название и автор.

Дальше обработчик подмешивает это описание в промпт и сохраняет в историю
чата пару «вопрос — ответ», поэтому DeepSeek помнит видео и в следующих
сообщениях.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

# Обычные ссылки, Shorts, live, embed и короткие youtu.be.
YOUTUBE_RE = re.compile(
    r"https?://(?:www\.|m\.|music\.)?"
    r"(?:"
    r"youtube\.com/(?:watch\?[^\s<>\"']*|shorts/[^\s<>\"']+|live/[^\s<>\"']+|"
    r"embed/[^\s<>\"']+|v/[^\s<>\"']+)"
    r"|youtu\.be/[^\s<>\"']+"
    r")",
    re.IGNORECASE,
)
SHORTS_RE = re.compile(r"(?i)/shorts/")

MAX_CONTEXT_CHARS = 9000      # сколько текста о видео отдаём модели
MAX_DESCRIPTION = 2500
MAX_TRANSCRIPT = 6000
FETCH_TIMEOUT = 45.0
DEFAULT_QUESTION = "Что в этом видео? Кратко перескажи основное."


class YouTubeError(Exception):
    """Понятная человеку ошибка: видео не удалось прочитать."""


@dataclass
class VideoInfo:
    """Всё, что удалось узнать о видео."""

    url: str
    title: str = ""
    uploader: str = ""
    duration: int = 0
    description: str = ""
    transcript: str = ""
    is_shorts: bool = False
    views: int = 0
    upload_date: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def context(self) -> str:
        """Текст для промпта: компактная «карточка» видео."""
        lines = ["[Видео с YouTube — данные, которые удалось прочитать]"]
        lines.append(f"Ссылка: {self.url}")
        if self.is_shorts:
            lines.append("Формат: YouTube Shorts (короткое видео)")
        if self.title:
            lines.append(f"Название: {self.title}")
        if self.uploader:
            lines.append(f"Автор/канал: {self.uploader}")
        if self.duration:
            lines.append(f"Длительность: {_format_duration(self.duration)}")
        if self.views:
            lines.append(f"Просмотров: {self.views:,}".replace(",", " "))
        if self.upload_date:
            lines.append(f"Опубликовано: {self.upload_date}")
        if self.description:
            lines.append(f"Описание: {self.description}")
        if self.transcript:
            lines.append(f"Расшифровка (субтитры):\n{self.transcript}")
        if not self.transcript:
            lines.append(
                "Субтитров нет — опирайся на название и описание. Если этого "
                "мало, честно скажи, что точного содержания не видно."
            )
        text = "\n".join(lines)
        return text[:MAX_CONTEXT_CHARS]


def video_in_url(text: str | None) -> str | None:
    """Первая ссылка на YouTube в сообщении (или None)."""
    if not text:
        return None
    match = YOUTUBE_RE.search(text)
    return _clean_url(match.group(0)) if match else None


def is_youtube_url(text: str | None) -> bool:
    return video_in_url(text) is not None


def question_from_text(text: str | None, url: str) -> str:
    """Убираем ссылку из сообщения — остаётся вопрос человека (или пусто)."""
    raw = (text or "").replace(url, " ")
    raw = re.sub(r"(?i)^\s*(?:посмотри|глянь|смотри|check|look at)\s+", "", raw)
    raw = " ".join(raw.split()).strip(" \t\r\n,.-—–:;!?«»\"'")
    return raw or DEFAULT_QUESTION


def _clean_url(url: str) -> str:
    """Убираем хвостовую пунктуацию и параметры, которые ломают oEmbed."""
    url = url.strip().rstrip(".,;:!?)»\"'")
    return url


def _format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# --- субтитры -------------------------------------------------------------


def _pick_subtitle_track(info: dict) -> dict | None:
    """Выбираем дорожку субтитров: ручная ru/en в приоритете, потом авто."""
    for group in ("subtitles", "automatic_captions"):
        tracks = info.get(group) or {}
        if not isinstance(tracks, dict):
            continue
        for lang in ("ru", "ru-RU", "en", "en-US", "en-GB"):
            entries = tracks.get(lang)
            if not entries:
                continue
            # json3 удобнее всего разбирать; vtt — запасной.
            for entry in entries:
                if str(entry.get("ext")) == "json3" and entry.get("url"):
                    return entry
            for entry in entries:
                if entry.get("url"):
                    return entry
    return None


def _parse_json3(payload: dict) -> str:
    lines: list[str] = []
    for event in payload.get("events") or []:
        segments = event.get("segs") or []
        text = "".join(str(seg.get("utf8") or "") for seg in segments)
        text = " ".join(text.split())
        if text:
            lines.append(text)
    return "\n".join(lines)


def _parse_vtt(text: str) -> str:
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line == "WEBVTT" or line.startswith(("NOTE", "STYLE", "REGION")):
            continue
        if "-->" in line or re.fullmatch(r"\d+", line):
            continue
        line = re.sub(r"<[^>]+>", "", line)
        line = " ".join(line.split())
        if line and (not lines or lines[-1] != line):
            lines.append(line)
    return "\n".join(lines)


def _download_subtitles(url: str) -> str:
    try:
        with httpx.Client(
            timeout=httpx.Timeout(25.0, connect=10.0),
            follow_redirects=True,
            headers={"User-Agent": "BobbyBot/1.0 (Telegram assistant)"},
        ) as client:
            response = client.get(url)
            response.raise_for_status()
            body = response.text
    except Exception as exc:  # noqa: BLE001
        log.info("YouTube: субтитры не скачались: %s", exc)
        return ""
    if url.endswith(".json3") or body.lstrip().startswith("{"):
        try:
            return _parse_json3(json.loads(body))
        except (ValueError, AttributeError):
            return ""
    return _parse_vtt(body)


# --- метаданные -----------------------------------------------------------


def _info_sync(url: str) -> dict:
    """yt-dlp без скачивания ролика: метаданные + ссылки на субтитры."""
    import yt_dlp

    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["ru", "en"],
        "subtitlesformat": "json3/vtt/best",
        "socket_timeout": 25,
        "extractor_retries": 2,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        return ydl.extract_info(url, download=False) or {}


def _oembed_sync(url: str) -> dict:
    """Запасной путь: официальный oEmbed (название и автор, без ключей)."""
    with httpx.Client(
        timeout=httpx.Timeout(20.0, connect=10.0),
        follow_redirects=True,
        headers={"User-Agent": "BobbyBot/1.0 (Telegram assistant)"},
    ) as client:
        response = client.get(
            "https://www.youtube.com/oembed", params={"url": url, "format": "json"}
        )
        response.raise_for_status()
        return response.json()


def _read_sync(url: str) -> VideoInfo:
    """Синхронная часть: yt-dlp → oEmbed → понятный результат."""
    is_shorts = bool(SHORTS_RE.search(url))
    info: dict = {}
    warning = ""
    try:
        info = _info_sync(url)
    except Exception as exc:  # noqa: BLE001 - YouTube режет серверные IP
        log.warning("YouTube: yt-dlp не смог прочитать %s: %s", url, exc)
        warning = str(exc)[:200]

    result = VideoInfo(
        url=url,
        title=str(info.get("title") or "").strip(),
        uploader=str(info.get("uploader") or info.get("channel") or "").strip(),
        duration=int(float(info.get("duration") or 0)),
        description=" ".join(str(info.get("description") or "").split())[:MAX_DESCRIPTION],
        is_shorts=is_shorts,
        views=int(info.get("view_count") or 0),
        upload_date=str(info.get("upload_date") or ""),
    )
    if result.upload_date and len(result.upload_date) == 8:
        result.upload_date = (
            f"{result.upload_date[:4]}-{result.upload_date[4:6]}-{result.upload_date[6:]}"
        )

    track = _pick_subtitle_track(info) if info else None
    if track and track.get("url"):
        transcript = _download_subtitles(str(track["url"]))
        result.transcript = transcript[:MAX_TRANSCRIPT]

    if not result.title:
        # yt-dlp не справился — хотя бы название через oEmbed.
        try:
            meta = _oembed_sync(url)
            result.title = str(meta.get("title") or "").strip()
            result.uploader = result.uploader or str(meta.get("author_name") or "").strip()
        except Exception as exc:  # noqa: BLE001
            log.warning("YouTube: oEmbed тоже не ответил: %s", exc)
            if warning:
                raise YouTubeError(
                    "Не смог прочитать видео. Возможно, оно приватное, удалено, "
                    "с ограничением по возрасту или YouTube не пустил сервер.\n"
                    f"Техническая причина: {warning}"
                ) from exc
            raise YouTubeError(
                "Не смог прочитать видео — проверь ссылку (или оно приватное)."
            ) from exc

    if warning and not result.transcript:
        result.warnings.append(warning)
    return result


async def describe_video(url: str) -> VideoInfo:
    """Асинхронная обёртка: читаем видео в отдельном потоке."""
    url = _clean_url(url)
    if not url:
        raise YouTubeError("Пустая ссылка — пришли ссылку на видео с YouTube 🙂")
    try:
        info = await asyncio.wait_for(
            asyncio.to_thread(_read_sync, url), timeout=FETCH_TIMEOUT + 30
        )
    except asyncio.TimeoutError as exc:
        raise YouTubeError(
            "YouTube слишком долго отвечает. Попробуй ещё раз через минуту."
        ) from exc
    if not info.title and not info.transcript:
        raise YouTubeError("Не удалось разобрать, что за видео по этой ссылке.")
    return info