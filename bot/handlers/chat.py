"""Главный обработчик: текст, картинка, видео-пост -> ответ модели."""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
import time

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from ..config import DEEPSEEK_WEB, Settings
from ..db import ROLE_ASSISTANT, ROLE_USER, Chat, Database, build_history
from ..imagegen import generate_image
from ..providers import ProviderRegistry
from ..providers.base import AuthRequired, Delta, Done, ProviderError, RateLimited
from ..service import ensure_active_chat, title_from_text
from ..streamer import THINKING, Streamer
from ..uav import (
    ai_level_note,
    answer_level_question,
    is_level_question,
    uav_enabled,
)
from ..videogen import VideoError, generate_video

router = Router(name="chat")
log = logging.getLogger(__name__)

FALLBACK_NOTE = "⚠️ {reason}\nОтвечаю через «{label}»."
PHOTO_QUESTION = "Что на этой картинке? Опиши подробно."
MAX_IMAGE_BYTES = 15 * 1024 * 1024

# Альбом (несколько фото в ОДНОМ сообщении) приходит как несколько отдельных
# сообщений с общим media_group_id. Владелец просил реагировать только на
# первое фото и молча пропускать остальные (а не отвечать «ещё отвечаю»).
MEDIA_GROUP_TTL = 600.0
_seen_media_groups: dict[str, float] = {}


def _is_extra_album_item(media_group_id: str | None) -> bool:
    """True, если это уже не первое сообщение альбома (его надо пропустить)."""
    if not media_group_id:
        return False
    now = time.monotonic()
    for key, seen_at in list(_seen_media_groups.items()):
        if now - seen_at > MEDIA_GROUP_TTL:
            del _seen_media_groups[key]
    if media_group_id in _seen_media_groups:
        return True
    _seen_media_groups[media_group_id] = now
    return False

# Вопросы «кто ты» отвечаем сами: некоторые модели упрямо называют себя
# DeepSeek/Gemini, даже когда в инструкции написано обратное.
IDENTITY_RE = re.compile(
    r"(?i)^\s*(?:"
    r"кто\s+ты(?:\s+так(?:ой|ая))?|ты\s+кто(?:\s+такой)?|"
    r"как\s+тебя\s+зовут|как\s+тво[её]\s+имя|тво[её]\s+имя|представься|представьтесь|"
    r"какая\s+(?:ты\s+)?модель|что\s+ты\s+за\s+модель|"
    r"на\s+какой\s+модели(?:\s+ты)?(?:\s+работаешь)?|"
    r"what\s+model(?:\s+are\s+you)?|who\s+are\s+you|your\s+name"
    r")\s*[?.!]*\s*$"
)
IDENTITY_ANSWER = (
    "Я Бобби — твой личный ассистент 🙂\n"
    "Помню контекст чата, смотрю картинки, рисую по описанию и умею искать в интернете."
)

IMAGE_NOUN_RE = re.compile(
    r"(?i)(картин\w*|изображени\w*|зображен\w*|фото\w*|рисун\w*|малюн\w*|"
    r"арт\w*|постер\w*|обложк\w*|иллюстрац\w*|"
    r"image|picture|photo|art|poster|illustration|wallpaper)"
)
DRAW_STRONG_RE = re.compile(
    r"(?i)\b(нарис\w*|рису\w*|намалю\w*|малю\w*|изобраз\w*|"
    r"draw|paint|sketch)\b"
)
DRAW_GENERIC_RE = re.compile(
    r"(?i)\b(сгенерир\w*|созда\w*|сдела\w*|згенеру\w*|створи\w*|зроби\w*|"
    r"generate|create|make)\b"
)
# «сгенерируй кота» — это про картинку, а «создай сайт» — нет.
STARTS_CREATIVE_RE = re.compile(
    r"(?i)^\s*(?:(?:бобби|bobby)\s*[,!.:—–-]*\s*)?"
    r"(?:пожалуйста[,\s]*)?(?:а\s+)?(?:ты\s+)?"
    r"(?:сгенерируй|сгенерируйте|сгенерируем|создай|создайте|создадим|"
    r"згенеруй|згенеруйте|створи|створіть|"
    r"generate|create)\b"
)
IMAGE_REQUEST_RE = re.compile(
    r"(?i)(хочу|хотел\w*|хотів|хотіла|нужн\w*|потрібн\w*|потр\w*|"
    r"треба|надо|давай|можешь|сможешь|можно|можеш|зможеш|"
    r"please|can\s+you|could\s+you)"
)
NON_IMAGE_WORDS = (
    "код", "json", "python", "скрипт", "текст", "стих", "таблиц",
    "план", "письм", "эссе", "сочинени", "документ", "сайт", "программ",
    "приложени", "презентац", "резюме", "список", "инструкц", "рецепт",
    "перевод", "конспект", "домашк", "задач", "анализ", "отчет", "отчёт",
    "email", "translate", "code", "script", "essay", "letter", "resume",
    "table", "list", "plan",
)
LEADING_IMAGE_RE = re.compile(
    r"(?i)^\s*(?:(?:бобби|bobby)\s*[,!.:—–-]*\s*)?"
    r"(?:пожалуйста[,\s]*)?(?:мне\s+|мені\s+)?"
    r"(?:картинк\w*|изображени\w*|зображен\w*|фото|рисун\w*|малюн\w*|"
    r"арт\w*|image|picture|photo|art)\b"
)
TEXT_ONLY_RE = re.compile(
    r"(?i)("
    # «я только текстовая модель», «я текстовая нейросеть»
    r"только\s+текстов\w*|"
    r"текстов(?:ая|ый|ой)\s+(?:модель|бот|ассистент|нейросет\w*)|"
    # «не умею / не могу рисовать, генерировать, создавать картинки»
    r"не\s+(?:умею|могу)\s+"
    r"(?:рисовать|нарисовать|генерировать|сгенерировать|создавать|создать)"
    r"(?:\s+(?:картинк\w*|изображени\w*|фото|арт))?|"
    r"не\s+имею\s+возможности\s+"
    r"(?:рисовать|нарисовать|генерировать|создавать|создать)|"
    r"text[- ]only|"
    r"(?:can(?:'|no)t|cannot|unable\s+to)\s+"
    r"(?:draw|generate|create)\s+(?:images?|pictures?|art)|"
    r"as\s+an?\s+(?:ai\s+)?(?:text|language)\s+model"
    r")"
)
CAPABILITY_RE = re.compile(
    r"(?i)^\s*(?:а\s+)?(?:ты\s+|ти\s+)?(?:умеешь|можешь|вмієш|можеш)\s+"
    r"(?:рисовать|нарисовать|генерировать|сгенерировать|создавать|создать|делать|сделать|"
    r"малювати|намалювати|генерувати|створювати|робити)"
    r"(?:\s+(?:картинки|картинку|изображения|изображение|фото|арт|рисунки|рисунок|"
    r"картинки|зображення|малюнки|малюнок))?"
    r"\s*\??\s*$"
)
CAPABILITY_ANSWER = (
    "Да, умею 🙂 Напиши, что нарисовать, например:\n"
    "«нарисуй закат на море» или /image кот в шлеме"
)

# --- генерация видео ------------------------------------------------------
# «Сгенерируй видео кота» — это видео, а не картинка. Проверяем ДО разбора
# картинок, иначе «сгенерируй видео...» уехало бы в рисовалку картинок.

VIDEO_NOUN_RE = re.compile(
    r"(?i)\b(видео|видеоролик|видосик|видос\w*|ролик|клип|анимаци\w*|"
    r"video|clip|animation)\b"
)
VIDEO_ACTION_RE = re.compile(
    r"(?i)\b(сгенерир\w*|созда\w*|сдела\w*|запиши|сними|нарисуй|"
    r"generate|create|make|render)\b"
)
# «скачай видео», «пришли видео», «что за видео» — это НЕ генерация.
VIDEO_NOT_GEN_RE = re.compile(
    r"(?i)\b(скач\w*|пришл\w*|загруз\w*|найди|download|посмотр\w*|"
    r"смотрел\w*|что\s+за|как\s+скачать)\b"
)


def _video_prompt(text: str) -> str | None:
    """Понимаем «сгенерируй видео ...» / /video ... — или None, если не про это."""
    low = text.lower().strip()
    if low.startswith(("/video", "/vid")):
        parts = text.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    if "youtube.com" in low or "youtu.be" in low:
        # Это ссылка на YouTube — её уже разобрал отдельный обработчик.
        return None
    if not VIDEO_NOUN_RE.search(low):
        return None
    if VIDEO_NOT_GEN_RE.search(low):
        return None
    if not VIDEO_ACTION_RE.search(low):
        return None

    prompt = re.sub(
        r"(?i)^\s*(?:(?:бобби|bobby)\s*[,!.:—–-]*\s*)?", "", text
    ).strip()
    prompt = re.sub(
        r"(?i)^(?:пожалуйста[,\s]*)?(?:а\s+)?(?:ты\s+)?"
        r"(?:можешь|сможешь|можно|давай|хочу|хотел(?:а)?)?\s*",
        "",
        prompt,
    ).strip()
    prompt = re.sub(
        r"(?i)^(?:сгенерир\w*|созда\w*|сдела\w*|запиши|сними|нарисуй|"
        r"generate|create|make|render)\s*",
        "",
        prompt,
    ).strip()
    prompt = re.sub(
        r"(?i)^(?:мне\s+)?(?:видео|видеоролик|видосик|видос\w*|ролик|клип|"
        r"анимацию|video|clip|animation)\b\s*[,:—–-]*\s*",
        "",
        prompt,
    ).strip()
    return prompt.strip(" .,!?:;—–-")

MODEL_NAMES = r"deepseek|дипсик|chatgpt|gpt[-\s]?\d\w*|openai|gemini|гемини|openrouter|nemotron|gemma|claude"
SELF_ID_RE = re.compile(
    rf"(?i)\b(я|меня\s+зовут|мо[её]\s+имя|i\s+am|i'm|my\s+name\s+is)"
    rf"\s*(?:—|-|:)?\s*(?:[a-zа-яё]+\s+){{0,4}}(?:{MODEL_NAMES})\b"
)


def _fix_identity(text: str) -> str:
    """Страховка: если модель всё же назвала себя чужой — правим на Бобби."""
    def replace(match: re.Match) -> str:
        head = match.group(1).lower()
        if head in {"i am", "i'm", "my name is"}:
            return "I am Bobby"
        return "Я Бобби"

    return SELF_ID_RE.sub(replace, text)


# DeepSeek при веб-поиске дописывает в ответ служебные маркеры источников:
# «[citation:1]», «[citation:]», иногда в китайских скобках. Владелец просил
# их не показывать — вырезаем и на всякий случай ещё раз перед отправкой
# (маркер может прийти куском в самом конце ответа).
CITATION_RE = re.compile(
    r"(?i)\[\s*citation\s*:?\s*[^\]]*\]|【\s*citation\s*[^】]*】"
)


def _strip_citations(text: str) -> str:
    cleaned = CITATION_RE.sub("", text)
    # Не оставляем «висячие» двойные пробелы и пробелы перед точкой.
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[ \t]+([.,;:!?])", r"\1", cleaned)
    return cleaned


def _mentions_uav_level(content) -> bool:
    """Есть ли в тексте упоминание уровня БПЛА/тревоги (для фильтра истории)."""
    if not isinstance(content, str):
        return False
    low = content.lower()
    if "уров" not in low:
        return False
    return any(
        word in low
        for word in ("бпла", "опасн", "тревог", "атак", "красн", "зел", "жёлт", "желт")
    )


@router.message(Command("image", "img"))
async def cmd_image(
    message: Message,
    db: Database,
    settings: Settings,
    registry: ProviderRegistry,
    locks: dict[int, asyncio.Lock],
) -> None:
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    prompt = parts[1].strip() if len(parts) > 1 else ""
    if not prompt:
        await message.answer(
            "Напиши, что нарисовать. Например:\n"
            "/image кот в космическом шлеме, акварель"
        )
        return
    await _locked(message, f"Нарисуй: {prompt}", db, settings, registry, locks)


@router.message(Command("video", "vid"))
async def cmd_video(
    message: Message,
    db: Database,
    settings: Settings,
    registry: ProviderRegistry,
    locks: dict[int, asyncio.Lock],
) -> None:
    """Быстрый путь: /video <описание> — сгенерировать короткое видео."""
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    prompt = parts[1].strip() if len(parts) > 1 else ""
    if not prompt:
        await message.answer(
            "Опиши, что показать в видео. Например:\n"
            "/video кот в космическом шлеме медленно плывёт среди звёзд\n\n"
            "Видео короткое (2–3 секунды) и бесплатное — генерация занимает "
            "до пары минут."
        )
        return
    await _locked(message, f"Сгенерируй видео: {prompt}", db, settings, registry, locks)


@router.message(F.text)
async def on_text(
    message: Message,
    db: Database,
    settings: Settings,
    registry: ProviderRegistry,
    locks: dict[int, asyncio.Lock],
) -> None:
    text = (message.text or "").strip()
    if not text:
        return
    if text.startswith("/"):
        await message.answer("Не знаю такую команду. Список команд — /help")
        return
    await _locked(message, text, db, settings, registry, locks)


@router.message(F.photo)
async def on_photo(
    message: Message,
    db: Database,
    settings: Settings,
    registry: ProviderRegistry,
    locks: dict[int, asyncio.Lock],
) -> None:
    """Фото, в том числе пересланный пост: картинка + подпись."""
    if _is_extra_album_item(message.media_group_id):
        # Альбом: реагируем ТОЛЬКО на первое фото, остальные молча пропускаем
        # (владелец просил не слать «ещё отвечаю» на каждое следующее).
        return
    photo = message.photo[-1] if message.photo else None
    if photo is None:
        return
    data = await _download(message, photo)
    if data is None:
        return
    caption = (message.caption or "").strip() or PHOTO_QUESTION
    await _locked(message, caption, db, settings, registry, locks, images=[data])


@router.message(F.video | F.animation | F.video_note)
async def on_video(
    message: Message,
    db: Database,
    settings: Settings,
    registry: ProviderRegistry,
    locks: dict[int, asyncio.Lock],
) -> None:
    """Пересланный пост с видео: видео пропускаем, текст (подпись) читаем."""
    caption = (message.caption or "").strip()
    if not caption:
        await message.answer(
            "🎬 Видео я не смотрю. Пришли текст или фото — с ними разберусь."
        )
        return
    # Само видео не скачиваем — отвечаем только по подписи.
    await _locked(message, caption, db, settings, registry, locks)


@router.message(F.document)
async def on_document(
    message: Message,
    db: Database,
    settings: Settings,
    registry: ProviderRegistry,
    locks: dict[int, asyncio.Lock],
) -> None:
    document = message.document
    mime = (document.mime_type or "") if document else ""
    if document is None:
        return
    if mime.startswith("video/"):
        # Видео файлом (бывает у пересланных постов): видео пропускаем,
        # подпись читаем.
        caption = (message.caption or "").strip()
        if not caption:
            await message.answer(
                "🎬 Видео я не смотрю. Пришли текст или фото — с ними разберусь."
            )
            return
        await _locked(message, caption, db, settings, registry, locks)
        return
    if not mime.startswith("image/"):
        await message.answer("Пока понимаю текст, фото и картинки файлом 🙂")
        return
    if _is_extra_album_item(message.media_group_id):
        # Альбом картинок файлом: тоже только первая.
        return
    data = await _download(message, document)
    if data is None:
        return
    caption = (message.caption or "").strip() or PHOTO_QUESTION
    await _locked(message, caption, db, settings, registry, locks, images=[data])


async def _download(message: Message, file) -> bytes | None:
    buffer = io.BytesIO()
    try:
        await message.bot.download(file, destination=buffer)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось скачать файл")
        await message.answer("😔 Не смог скачать картинку. Попробуй ещё раз.")
        return None
    data = buffer.getvalue()
    if not data:
        await message.answer("😔 Картинка пришла пустой. Попробуй ещё раз.")
        return None
    if len(data) > MAX_IMAGE_BYTES:
        await message.answer("Картинка слишком большая (больше 15 МБ). Пришли поменьше.")
        return None
    return data


async def _locked(
    message: Message,
    text: str,
    db: Database,
    settings: Settings,
    registry: ProviderRegistry,
    locks: dict[int, asyncio.Lock],
    *,
    images: list[bytes] | None = None,
    context: str | None = None,
) -> None:
    user_id = message.from_user.id
    lock = locks.setdefault(user_id, asyncio.Lock())
    if lock.locked():
        await message.answer("⏳ Ещё отвечаю на предыдущее сообщение — секунду.")
        return

    async with lock:
        try:
            await _respond(
                message, text, user_id, db, settings, registry,
                images=images, context=context,
            )
        except Exception:  # noqa: BLE001
            log.exception("Не удалось обработать сообщение")
            try:
                await message.answer("😔 Что-то пошло не так. Попробуй ещё раз.")
            except Exception:
                pass


def _image_url(data: bytes, mime: str = "image/jpeg") -> dict:
    encoded = base64.b64encode(data).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{encoded}"},
    }


def _image_prompt(text: str) -> str | None:
    """Понимаем «нарисуй кота», «можешь сгенерировать фото заката», /image …"""
    low = text.lower().strip()
    if low.startswith(("/image", "/img")):
        parts = text.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    has_noun = bool(IMAGE_NOUN_RE.search(low))
    has_strong = bool(DRAW_STRONG_RE.search(low))
    has_generic = bool(DRAW_GENERIC_RE.search(low))
    has_request = bool(IMAGE_REQUEST_RE.search(low))
    has_other_task = any(w in low for w in NON_IMAGE_WORDS)
    starts_creative = bool(STARTS_CREATIVE_RE.match(low))

    # «нарисуй …», «можешь нарисовать …» — рисуем даже без слова «картинка».
    # «сгенерируй/создай кота» в начале фразы — тоже картинка, если человек
    # не просит код/текст/сайт. «сделай/создай …» с явным словом картинки
    # срабатывает всегда.
    triggered = (
        has_strong
        or (has_generic and has_noun)
        or (has_noun and has_request)
        or (starts_creative and not has_other_task)
    )
    if not triggered:
        return None
    if has_other_task and not has_noun:
        return None

    prompt = re.sub(
        r"(?i)^\s*(?:(?:бобби|bobby)\s*[,!.:—–-]*\s*)?"
        r"(?:пожалуйста[,\s]*)?"
        r"(?:(?:а\s+)?(?:ты\s+)?(?:можешь|сможешь|можно|давай|хочу|хотел(?:а)?|"
        r"можеш|зможеш|хотів|хотіла|треба)\s+)?"
        r"(?:мне\s+|мені\s+)?"
        r"(?:нарисовать|нарисуй(?:-ка)?|нарисуйте|нарисуем|нарисуешь|нарисуете|рисуй|рисуйте|"
        r"намалювати|намалюй|намалюйте|малюй|малюйте|"
        r"изобразить|изобрази|изобразите|"
        r"сгенерировать|сгенерируем|сгенерируй|сгенерируйте|"
        r"згенерувати|згенеруй|згенеруйте|"
        r"создать|создадим|создай|создайте|"
        r"створити|створи|створіть|"
        r"сделать|сделаем|сделай|сделайте|"
        r"зробити|зроби|зробіть|"
        r"draw|generate|create|make|paint|sketch)"
        r"\s*[:,—–-]*\s*",
        "",
        text,
    ).strip()
    # «хочу картинку…», «давай кота…» — убираем и такие зачины без глагола.
    prompt = re.sub(
        r"(?i)^(?:а\s+)?(?:ты\s+)?(?:пожалуйста[,\s]*)?"
        r"(?:можешь|сможешь|можно|давай|хочу|хотел(?:а)?|"
        r"можеш|зможеш|хотів|хотіла|треба)\s+",
        "",
        prompt,
    ).strip()
    prompt = re.sub(r"(?i)^(?:мне|мені)\s+", "", prompt).strip()
    prompt = re.sub(r"(?i)^(?:пожалуйста[,\s]*)", "", prompt).strip()
    prompt = re.sub(
        r"(?i)^(?:нужна|нужен|нужно|потрібна|потрібен|потрібно)\s+",
        "",
        prompt,
    ).strip()
    normalized = prompt.strip(" .,!?:;—–-").lower()
    if normalized in {
        "", "картинку", "картинка", "картинки", "изображение", "изображения",
        "фото", "рисунок", "арт", "image", "picture", "photo", "art",
        "малюнок", "зображення",
    }:
        return ""
    return prompt


def _fallback_prompt(text: str) -> str:
    """Запасной вариант, если фразу не разобрал основной разборщик."""
    prompt = re.sub(r"(?i)^\s*(?:бобби|bobby)\s*[,!.:—–-]*\s*", "", text).strip()
    prompt = re.sub(r"(?i)^(?:пожалуйста[,\s]*)", "", prompt).strip()
    prompt = re.sub(
        r"(?i)^(?:нарисуй(?:-ка)?|нарисуйте|рисуй|рисуйте|намалюй|намалюйте|"
        r"сгенерируй|сгенерируйте|згенеруй|згенеруйте|создай|создайте|"
        r"створи|створіть|сделай|сделайте|зроби|зробіть|"
        r"draw|generate|create|make|paint|sketch)\s*[:,—–-]*\s*",
        "",
        prompt,
    ).strip()
    return prompt or text


def _wants_image(text: str) -> bool:
    low = text.lower()
    if (
        DRAW_STRONG_RE.search(low)
        or STARTS_CREATIVE_RE.match(low)
        or LEADING_IMAGE_RE.match(low)
    ):
        return True
    return bool(IMAGE_NOUN_RE.search(low) and IMAGE_REQUEST_RE.search(low))


async def _respond(
    message: Message,
    text: str,
    user_id: int,
    db: Database,
    settings: Settings,
    registry: ProviderRegistry,
    *,
    images: list[bytes] | None = None,
    context: str | None = None,
) -> None:
    chat = await ensure_active_chat(db, user_id, settings)

    # Чат мог быть создан, когда провайдер был настроен, а теперь ключ убрали.
    if settings.provider(chat.provider) is None:
        fallback = settings.fallback
        if fallback is None:
            await message.answer(
                "Провайдер этого чата больше не настроен. Выбери доступный в /settings."
            )
            return
        chat = await db.update_chat(
            chat.id, provider=fallback.key, model=fallback.model, ds_cid=None
        )

    is_first = await db.message_count(chat.id) == 0

    # 0. Вопрос про уровень БПЛА — отвечаем точно и мгновенно, без модели.
    #    В историю НЕ пишем: выключил оповещения → ИИ не должен знать уровни.
    if not images and is_level_question(text):
        answer = await answer_level_question(db, user_id)
        await message.answer(answer)
        await _finish_turn(db, chat, is_first, text)
        return

    await db.add_message(chat.id, ROLE_USER, text)

    # 1. «Кто ты?» — отвечаем сами, чтобы никакая модель не назвала себя DeepSeek.
    if not images and IDENTITY_RE.match(text):
        await message.answer(IDENTITY_ANSWER)
        await db.add_message(chat.id, ROLE_ASSISTANT, IDENTITY_ANSWER)
        await _finish_turn(db, chat, is_first, text)
        return

    # 2. «Умеешь рисовать?» — отвечаем сами, чтобы модель не сказала «я текстовый».
    if not images and CAPABILITY_RE.match(text):
        await message.answer(CAPABILITY_ANSWER)
        await db.add_message(chat.id, ROLE_ASSISTANT, CAPABILITY_ANSWER)
        await _finish_turn(db, chat, is_first, text)
        return

    # 3. «Сгенерируй видео ...» — делаем видео (проверяем РАНЬШЕ картинок).
    if not images:
        video_prompt = _video_prompt(text)
        if video_prompt is not None:
            if not video_prompt:
                await message.answer(
                    "Что показать в видео? Напиши, например: «сгенерируй видео "
                    "кот в космосе» или /video кот в космосе"
                )
                await db.touch_chat(chat.id)
                return
            await _make_video(message, chat, video_prompt, db, settings, is_first)
            return

    # 4. «Нарисуй ...» — генерируем картинку и присылаем её.
    if not images:
        prompt = _image_prompt(text)
        if prompt is not None:
            if not prompt:
                await message.answer(
                    "Что нарисовать? Напиши, например: «нарисуй закат на море» "
                    "или /image кот в космическом шлеме"
                )
                await db.touch_chat(chat.id)
                return
            await _draw(message, chat, text, prompt, db, settings, is_first)
            return

    history = await db.messages(chat.id)
    messages = build_history(
        history,
        max_messages=settings.memory_max_messages,
        max_chars=settings.memory_max_chars,
    )
    uav_on = await uav_enabled(db, user_id)
    if not uav_on:
        # Оповещения выключены → ИИ не должен видеть уровни даже в старой
        # истории (например, если раньше бот упоминал «уровень БПЛА»).
        messages = [
            message
            for message in messages
            if not (
                message.get("role") == ROLE_ASSISTANT
                and _mentions_uav_level(message.get("content"))
            )
        ]

    if images and messages:
        # Картинку прикладываем только к текущей реплике: в истории она не нужна.
        parts: list[dict] = [{"type": "text", "text": text}]
        parts += [_image_url(data) for data in images]
        messages[-1] = {"role": "user", "content": parts}

    # Скрытый контекст (данные о видео с YouTube) и текущий уровень БПЛА
    # подмешиваем ТОЛЬКО к этому запросу — в историю он не попадает.
    level_note = await ai_level_note(db, user_id, enabled=uav_on)
    notes = [note for note in (context, level_note) if note]
    if notes and messages:
        note_text = "\n\n".join(notes)
        content = messages[-1].get("content")
        if isinstance(content, list):
            messages[-1] = {
                "role": "user",
                "content": [{"type": "text", "text": note_text}, *content],
            }
        else:
            messages[-1] = {
                "role": "user",
                "content": f"{note_text}\n\n{content or ''}",
            }

    # Кто отвечает: обычный провайдер чата или «зрячая» модель для картинок.
    used_provider = chat.provider
    model, cid, thinking, search = _provider_params(chat, used_provider, settings)

    if images:
        vision = settings.vision_config()
        if vision is None:
            await message.answer(
                "Картинки пока не могу: не настроена модель со зрением.\n"
                "Добавь бесплатный ключ OpenRouter — и я начну видеть фото."
            )
            await db.touch_chat(chat.id)
            return
        used_provider = vision.key
        model, cid, thinking, search = vision.model, None, False, False

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    placeholder = await message.answer(THINKING)
    streamer = Streamer(message.bot, message.chat.id, placeholder.message_id)

    new_cid = cid
    error: ProviderError | None = None

    try:
        new_cid = await _run(
            used_provider,
            messages,
            model=model,
            cid=cid,
            thinking=thinking,
            search=search,
            streamer=streamer,
            registry=registry,
        )
    except ProviderError as exc:
        error = exc

    # Если основной провайдер не смог начать ответ — пробуем резервную модель.
    # Для картинок резерв не годится: текстовая модель их не увидит.
    if error is not None and not streamer.text and not images:
        fallback = settings.fallback
        if fallback is not None and fallback.key != used_provider:
            await message.answer(FALLBACK_NOTE.format(reason=error.message, label=fallback.label))
            used_provider = fallback.key
            new_cid = None
            try:
                new_cid = await _run(
                    fallback.key,
                    messages,
                    model=fallback.model,
                    cid=None,
                    thinking=False,
                    search=False,
                    streamer=streamer,
                    registry=registry,
                )
                error = None
            except ProviderError as exc:
                error = exc

    if not streamer.text:
        await streamer.delete_placeholder()
        await message.answer(_error_text(error))
        await db.touch_chat(chat.id)
        return

    # Страховка: модель иногда отвечает «я только текстовый», хотя человек
    # явно просит картинку. Тогда рисуем сами, а текстовый отказ убираем.
    if not images and TEXT_ONLY_RE.search(streamer.text) and _wants_image(text):
        prompt = _image_prompt(text)
        if prompt is None:
            prompt = _fallback_prompt(text)
        log.info("Модель отказалась рисовать — включаю генерацию картинки")
        await streamer.delete_placeholder()
        if not prompt:
            await message.answer(
                "Что нарисовать? Напиши, например: «нарисуй закат на море» "
                "или /image кот в космическом шлеме"
            )
            await db.touch_chat(chat.id)
            return
        await _draw(message, chat, text, prompt, db, settings, is_first)
        return

    streamer.text = _strip_citations(_fix_identity(streamer.text))
    await streamer.finish()
    await db.add_message(chat.id, ROLE_ASSISTANT, streamer.text)

    fields: dict = {}
    if used_provider == DEEPSEEK_WEB and error is None:
        fields["ds_cid"] = new_cid
    elif not images:
        fields["ds_cid"] = None
    if is_first and chat.title == "Новый чат":
        fields["title"] = title_from_text(text)
    await db.update_chat(chat.id, **fields)
    await db.touch_chat(chat.id)

    if error is not None:
        await message.answer(f"⚠️ Ответ оборвался: {error.message}")


async def _draw(
    message: Message,
    chat: Chat,
    text: str,
    prompt: str,
    db: Database,
    settings: Settings,
    is_first: bool,
) -> None:
    """Рисуем картинку и отправляем её в чат."""
    try:
        await message.bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_PHOTO)
    except Exception:  # noqa: BLE001
        pass
    placeholder = await message.answer("🎨 Рисую…")
    try:
        data = await generate_image(prompt, settings)
    except ProviderError as exc:
        log.warning("Не удалось нарисовать картинку: %s", exc.message)
        await placeholder.edit_text(f"🎨 {exc.message}")
        await db.touch_chat(chat.id)
        return
    except Exception:  # noqa: BLE001
        log.exception("Ошибка генерации картинки")
        await placeholder.edit_text("🎨 Не получилось нарисовать. Попробуй ещё раз.")
        await db.touch_chat(chat.id)
        return

    if data.startswith(b"\x89PNG"):
        filename = "bobby.png"
    elif data.startswith(b"RIFF"):
        filename = "bobby.webp"
    else:
        filename = "bobby.jpg"
    try:
        await message.answer_photo(
            BufferedInputFile(data, filename=filename),
            caption=f"🎨 {prompt[:900]}",
        )
    except Exception:  # noqa: BLE001
        log.exception("Не удалось отправить картинку в Telegram")
        await placeholder.edit_text("🎨 Картинка готова, но Telegram её не принял. Попробуй ещё раз.")
        await db.touch_chat(chat.id)
        return

    await placeholder.delete()
    await db.add_message(chat.id, ROLE_ASSISTANT, f"🎨 Нарисовал картинку: {prompt}")
    if is_first and chat.title == "Новый чат":
        await db.update_chat(chat.id, title=title_from_text(f"🎨 {prompt}"))
    await db.touch_chat(chat.id)


async def _make_video(
    message: Message,
    chat: Chat,
    prompt: str,
    db: Database,
    settings: Settings,
    is_first: bool,
) -> None:
    """Генерируем короткое видео и отправляем его в чат."""
    try:
        await message.bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_VIDEO)
    except Exception:  # noqa: BLE001
        pass
    placeholder = await message.answer(
        "🎬 Генерирую видео… Обычно 1–2 минуты (иногда дольше), не уходи 🙂"
    )
    try:
        data = await generate_video(prompt, settings)
    except VideoError as exc:
        log.warning("Не удалось сгенерировать видео: %s", exc.message)
        await placeholder.edit_text(f"🎬 {exc.message}")
        await db.touch_chat(chat.id)
        return
    except Exception:  # noqa: BLE001
        log.exception("Ошибка генерации видео")
        await placeholder.edit_text("🎬 Не получилось сгенерировать видео. Попробуй ещё раз.")
        await db.touch_chat(chat.id)
        return

    filename = "bobby.webm" if data[:4] == b"\x1aE\xdf\xa3" else "bobby.mp4"
    video_file = BufferedInputFile(data, filename=filename)
    sent = False
    try:
        await message.answer_video(video_file, caption=f"🎬 {prompt[:900]}")
        sent = True
    except Exception as exc:  # noqa: BLE001 - Telegram не принял как видео
        log.warning("Не удалось отправить видео (%s) — шлю файлом", exc)
    if not sent:
        try:
            await message.answer_document(
                BufferedInputFile(data, filename=filename),
                caption=f"🎬 {prompt[:900]}",
            )
        except Exception:  # noqa: BLE001
            log.exception("Не удалось отправить видео файлом")
            await placeholder.edit_text(
                "🎬 Видео готово, но Telegram его не принял. Попробуй ещё раз."
            )
            await db.touch_chat(chat.id)
            return

    await placeholder.delete()
    await db.add_message(chat.id, ROLE_ASSISTANT, f"🎬 Сгенерировал видео: {prompt}")
    if is_first and chat.title == "Новый чат":
        await db.update_chat(chat.id, title=title_from_text(f"🎬 {prompt}"))
    await db.touch_chat(chat.id)


async def _finish_turn(db: Database, chat: Chat, is_first: bool, text: str) -> None:
    if is_first and chat.title == "Новый чат":
        await db.update_chat(chat.id, title=title_from_text(text))
    await db.touch_chat(chat.id)


def _provider_params(
    chat: Chat, key: str, settings: Settings
) -> tuple[str, str | None, bool, bool]:
    if key == DEEPSEEK_WEB:
        return chat.model, chat.ds_cid, chat.thinking, chat.search
    cfg = settings.provider(key)
    return (cfg.model if cfg else ""), None, False, False


async def _run(
    provider_key: str,
    messages: list[dict],
    *,
    model: str,
    cid: str | None,
    thinking: bool,
    search: bool,
    streamer: Streamer,
    registry: ProviderRegistry,
) -> str | None:
    provider = registry.get(provider_key)
    new_cid: str | None = cid
    async for event in provider.stream(
        messages, cid=cid, model=model, thinking=thinking, search=search
    ):
        if isinstance(event, Delta):
            await streamer.push(event.text)
        elif isinstance(event, Done):
            new_cid = event.cid
    return new_cid


def _error_text(error: ProviderError | None) -> str:
    if error is None:
        return "Не получил ответ от модели. Попробуй ещё раз."
    prefix = "⏳ " if isinstance(error, RateLimited) else "⚠️ "
    text = prefix + error.message
    if isinstance(error, AuthRequired):
        text += (
            "\n\nКак починить: на компьютере, где запущен бот, выполни "
            "`./scripts/ds_login.sh` и залогинься в DeepSeek заново."
        )
    return text


@router.message()
async def on_unsupported(message: Message) -> None:
    await message.answer(
        "Пока понимаю текст, фото и картинки файлом 🙂\n"
        "Голосовые, кружки и стикеры — не умею."
    )
