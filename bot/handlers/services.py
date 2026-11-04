"""Сервисы: меню раздела и услуги — музыка (SoundCloud/ВКонтакте), ТикТок.

Схема музыки (бережёт слабый сервер; ок, как просил хозяин):
1. человек говорит, что хочет музыку — кнопкой «Сервисы», командой
   /music (СаундКлауд), /vkmusic (ВК) или просто текстом «скачай песню …»;
2. бот спрашивает, ГДЕ качать (🌫 SoundCloud или 🇷🇺 ВКонтакте), если
   источник ещё не ясен (для текстовых запросов источник спрашивается, у
   команд он очевиден из самой команды);
3. потом просит название, если названия ещё нет;
4. поиск: до 10 треков кнопками (без скачивания), выбор кнопкой;
5. качается только выбранный трек и отправляется в чат; список кнопок
   после этого закрывается.

ТикТок: бросил ссылку в любое сообщение (например, «скачай это
https://vm.tiktok.com/…») — бот отправляет mp4 без водяного знака.

YouTube: бросил ссылку (обычную, Shorts или youtu.be) — бот читает название,
описание и субтитры, отвечает по видео и запоминает его в текущем чате.
Видео-генерация: «сгенерируй видео ...» или /video — короткий ролик
(2–3 секунды) из бесплатного HF-пространства.
"""

from __future__ import annotations

import logging
import re
import time

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from ..config import Settings
from ..db import ROLE_ASSISTANT, ROLE_USER, Database
from ..keyboards import (
    BTN_CHATS,
    BTN_NEW,
    BTN_SERVICES,
    BTN_SETTINGS,
    music_choices_keyboard,
    music_source_keyboard,
    services_keyboard,
)
from ..music import MusicError, Track, download_track
from ..music import find_tracks as find_sc_tracks
from ..service import ensure_active_chat
from ..tiktok import TikTokError, download_video, video_in_url
from ..youtube import YouTubeError, describe_video, question_from_text
from ..youtube import video_in_url as youtube_in_url
from ..vkmusic import (
    VK_TOKEN_KV_KEY,
    download_track as vk_download_track,
    find_tracks as find_vk_tracks,
    oauth_url,
    parse_login_result,
    remember_db_token,
    vk_configured,
)

router = Router(name="services")
log = logging.getLogger(__name__)

SEARCHING = "🔎 Ищу «{query}»…"
CHOOSING = "«{query}» — вот что нашлось. Жми на трек 👇"
DOWNLOADING = "⬇️ Скачиваю: «{title}» — {performer}\nЭто займёт немного времени…"
TIKTOK_DOWNLOADING = "⬇️ Скачиваю видео с ТикТока без водяного знака…"
ASK_NAME = (
    "🎵 Напиши название трека или исполнителя — покажу 10 вариантов, "
    "выбери нужный."
    "\n\nОтменить: /cancel"
)
ASK_SOURCE = "🎵 Где качаем?"
SOURCE_LABELS = {"sc": "🎧 SoundCloud", "vk": "🇷🇺 ВКонтакте"}


class MusicStates(StatesGroup):
    """Пошаговый сценарий: источник → название → выбор трека → скачивание."""

    choosing_source = State()  # ждём кнопку источника (запрос может уже лежать)
    waiting_query = State()    # источник выбран, ждём название трека
    choosing = State()         # ждём кнопку с треком из списка
    waiting_vk_url = State()   # /vklogin: ждём адрес страницы blank.html


def _button_labels(tracks: list[Track]) -> list[str]:
    return [f"{track.title} — {track.performer}" for track in tracks]


async def _finding_for_source(
    source: str, query: str, db: Database | None = None
) -> list[Track]:
    """Ищем треки в выбранном источнике."""
    if source == "vk":
        return await find_vk_tracks(query, db=db)
    return await find_sc_tracks(query)


async def _download_for_source(
    source: str, track: Track
) -> tuple[bytes, str, int]:
    """Скачиваем выбранный трек в выбранном источнике."""
    if source == "vk":
        return await vk_download_track(track)
    return await download_track(track)


async def _deliver(
    message: Message,
    chat,
    track: Track,
    query: str,
    db: Database,
    settings: Settings,
    source: str = "sc",
    placeholder: Message | None = None,
) -> None:
    """Качаем выбранный трек и отправляем аудио."""
    source_label = SOURCE_LABELS.get(source, "🎧 SoundCloud")
    status: Message | None = placeholder
    if status is None:
        status = await message.answer(
            DOWNLOADING.format(title=track.title, performer=track.performer)
        )
    else:
        try:
            await status.edit_text(
                DOWNLOADING.format(title=track.title, performer=track.performer)
            )
        except TelegramBadRequest:
            pass

    try:
        data, filename, duration = await _download_for_source(source, track)
    except MusicError as exc:
        text = f"🎵 {source_label}: {exc}"
        try:
            await status.edit_text(text)
        except TelegramBadRequest:
            await message.answer(text)
        await db.touch_chat(chat.id)
        return

    try:
        await status.delete()
    except Exception:  # noqa: BLE001
        pass

    audio_file = BufferedInputFile(data, filename=filename)
    try:
        await message.answer_audio(
            audio_file,
            title=track.title[:64],
            performer=track.performer[:64],
            duration=duration,
        )
    except Exception as exc:  # noqa: BLE001 - Telegram не принял как аудио
        log.warning("Не удалось отправить аудио (%s) — шлю файлом", exc)
        try:
            await message.answer_document(
                BufferedInputFile(data, filename=filename),
                caption=f"🎧 {track.title} — {track.performer}",
            )
        except Exception:
            log.exception("Не удалось отправить трек")
            await message.answer("😔 Трек скачался, но Telegram его не принял.")
            await db.touch_chat(chat.id)
            return

    # Трек доставлен — список кнопок больше не нужен, закрываем чат от хлама.
    try:
        await message.delete()
    except Exception:  # noqa: BLE001
        pass

    await db.add_message(chat.id, ROLE_USER, f"🎵 Музыка по запросу: {query}")
    await db.add_message(
        chat.id,
        ROLE_ASSISTANT,
        f"🎧 Отправил трек ({source_label}): «{track.title}» — {track.performer}",
    )
    await db.touch_chat(chat.id)


async def _show_choices(
    message: Message,
    query: str,
    state: FSMContext,
    settings: Settings,
    db: Database | None = None,
) -> None:
    """Лёгкий поиск + предложить выбрать трек кнопкой. Источник берём из FSM."""
    data = await state.get_data()
    source = data.get("source") or "sc"
    label = SOURCE_LABELS.get(source, "🎧 SoundCloud")
    started = time.monotonic()
    log.info("поиск[%s] %r: начат", source, query)

    placeholder: Message = await message.answer(SEARCHING.format(query=query))
    try:
        tracks = await _finding_for_source(source, query, db=db)
    except MusicError as exc:
        text = f"🎵 {label} {exc}".strip()
        try:
            await placeholder.edit_text(text)
        except TelegramBadRequest:
            await message.answer(text)
        return
    except Exception as exc:  # noqa: BLE001 - база/сеть: человек должен увидеть ответ
        log.exception("поиск[%s] %r: неожиданная ошибка", source, query)
        text = (
            f"🎵 {label}: что-то сломалось при поиске "
            f"({type(exc).__name__}). Попробуй ещё раз — и это повторится, "
            "скажи мне. "
        )
        try:
            await placeholder.edit_text(text)
        except TelegramBadRequest:
            await message.answer(text)
        return

    log.info(
        "поиск[%s] %r: %d треков за %.1fс",
        source, query, len(tracks), time.monotonic() - started,
    )

    try:
        await placeholder.delete()
    except Exception:  # noqa: BLE001
        pass

    await state.set_state(MusicStates.choosing)
    await state.update_data(
        tracks=[track.__dict__ for track in tracks], query=query
    )
    keyboard = music_choices_keyboard(_button_labels(tracks))
    await message.answer(CHOOSING.format(query=query), reply_markup=keyboard)


async def _send_track(
    message: Message,
    query: str,
    state: FSMContext,
    settings: Settings,
    source: str,
    db: Database | None = None,
) -> None:
    """Укороченный путь для команд /music, /vkmusic: сразу к списку треков."""
    await state.update_data(source=source)
    await _show_choices(message, query, state, settings, db=db)


@router.message(Command("music"))
async def cmd_music(
    message: Message,
    command: CommandObject,
    settings: Settings,
    state: FSMContext,
    db: Database,
) -> None:
    """Быстрый путь: /music <запрос> — качаем из SoundCloud по умолчанию."""
    query = (command.args or "").strip()
    await state.clear()
    if not query:
        await message.answer(
            "🎵 Напиши так:\n/music название трека или исполнитель\n"
            "Например: /music скриптонит это любовь\n\n"
            "Для ВКонтакте: /vkmusic <запрос> (если VK не привязан — /vklogin)"
        )
        return
    await _send_track(message, query, state, settings, source="sc", db=db)


@router.message(Command("vkmusic", "vk"))
async def cmd_vkmusic(
    message: Message,
    command: CommandObject,
    settings: Settings,
    state: FSMContext,
    db: Database,
) -> None:
    """Быстрый путь: /vk <запрос> — качаем из ВКонтакте по аккаунту владельца."""
    query = (command.args or "").strip()
    await state.clear()
    if not query:
        await message.answer(
            "🎵 Напиши так:\n/vkmusic название трека или исполнитель\n"
            "Например: /vkmusic Три дня дождя привяжи меня\n\n"
            "VK ещё не привязан? Тогда /vklogin — по ссылке. Но для музыки "
            "это теперь не главное: с весны 2026 VK ищет музыку только по "
            "входу логин/пароль — добавь VK_LOGIN и VK_PASSWORD в Render."
        )
        return
    await _send_track(message, query, state, settings, source="vk", db=db)


# Альтернативные названия той же команды.
@router.message(Command("song", "track"))
async def cmd_song_alias(
    message: Message,
    command: CommandObject,
    settings: Settings,
    state: FSMContext,
    db: Database,
) -> None:
    query = (command.args or "").strip()
    if not query:
        await message.answer("🎵 Напиши: /song название трека или исполнитель")
        return
    await _send_track(message, query, state, settings, source="sc", db=db)


# /vklogin — привязка VK без пароля: открыли страницу в браузере,
# подтвердили вход, прислали боту адрес blank.html с access_token.
@router.message(Command("vklogin"))
async def cmd_vklogin(
    message: Message,
    state: FSMContext,
    db: Database,
) -> None:
    await state.clear()
    await state.set_state(MusicStates.waiting_vk_url)
    await message.answer(
        "🔗 Привяжем ВКонтакте без пароля — по ссылке.\n\n"
        "1. Открой в браузере (на ноутбуке удобно):\n"
        f"{oauth_url()}\n\n"
        "2. Войди в VK как получится: по коду из SMS, по QR-коду с телефона "
        "или как предложит сайт.\n"
        "3. VK спросит «Разрешить доступ?» — подтверди.\n"
        "4. Откроется страница, где почти ничего нет (blank.html). Скопируй "
        "её адрес из строки браузера — там будет access_token=… — и пришли "
        "его сюда одним сообщением.\n\n"
        "Токен сохранится в базе и бот будет качать музыку из ВК без "
        "логина и пароля. Отменить: /cancel"
    )


@router.message(MusicStates.waiting_vk_url, F.text)
async def vk_url_message(
    message: Message,
    state: FSMContext,
    db: Database,
) -> None:
    """Ждём адрес blank.html c access_token (или любой текст с токеном)."""
    text = message.text or ""
    if text.startswith("/"):
        await state.clear()
        await message.answer("Ок, привязку ВКонтакте отменил. Пиши что угодно 🙌")
        return

    token, user_id = parse_login_result(text)
    if not token:
        await state.set_state(MusicStates.waiting_vk_url)
        await message.answer(
            "Не вижу access_token в этом сообщении 🤔\nСкопируй адрес страницы "
            "целиком (там будет access_token=…) и пришли ещё раз. "
            "Отмена — /cancel"
        )
        return

    await db.kv_set(VK_TOKEN_KV_KEY, token)
    remember_db_token(token)
    await state.clear()
    await message.answer(
        f"✅ ВКонтакте привязан{f' (id {user_id})' if user_id else ''}!\n"
        "⚠️ Весной 2026 VK закрыл поиск музыки для токенов привязки — если "
        "трек не найдётся, добавь VK_LOGIN и VK_PASSWORD в переменные "
        "Render: они всегда в приоритете.\nПопробуй: /vk <трек>."
    )


# /cancel — выход из сценария поиска музыки.
@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Ок 🙌 Пиши что угодно — я просто отвечаю.")


@router.message(F.text == BTN_SERVICES)
async def services_menu(message: Message) -> None:
    await message.answer(
        "🎧 Сервисы\n\nПока умею:\n"
        "• 🎵 Музыка — найду трек по названию или исполнителю и пришлю аудио\n"
        "  (источники: 🎧 SoundCloud / 🇷🇺 ВКонтакте).\n"
        "• 🎬 ТикТок — пришли ссылку, пришлю видео без водяного знака.\n"
        "• 📺 YouTube — кинь ссылку (можно Shorts), посмотрю и перескажу.\n"
        "• 🎞 Видео — «сгенерируй видео ...» или /video: сделаю короткий ролик.",
        reply_markup=services_keyboard(),
    )


@router.message(Command("services"))
async def cmd_services(message: Message) -> None:
    await services_menu(message)


@router.callback_query(F.data == "svc:music")
async def cb_music(callback: CallbackQuery, state: FSMContext) -> None:
    """Начало сценария музыки: спрашиваем источник."""
    await callback.answer()
    await state.clear()
    await state.set_state(MusicStates.choosing_source)
    await state.update_data(query="")
    if callback.message:
        await callback.message.edit_text(
            ASK_SOURCE,
            reply_markup=music_source_keyboard(),
        )


@router.callback_query(F.data == "svc:tiktok")
async def cb_tiktok(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    if callback.message:
        await callback.message.edit_text(
            "🎬 Просто пришли ссылку на видео из ТикТока (обычную или "
            "короткую vm.) — пришлю mp4 без водяного знака.\n\n"
            "Это работает в любой момент, команду выбирать не нужно."
        )


@router.callback_query(F.data == "svc:video")
async def cb_video(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    if callback.message:
        await callback.message.edit_text(
            "🎞 Сгенерирую короткое видео по описанию.\n\n"
            "Напиши так: «сгенерируй видео кот в космическом шлеме, звёзды, "
            "кинематографично»\nили командой: /video <описание>.\n\n"
            "Ролик короткий (2–3 секунды), бесплатный; генерация занимает "
            "до пары минут."
        )


@router.callback_query(F.data == "svc:close")
async def cb_services_close(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        try:
            await callback.message.delete()
        except Exception:  # noqa: BLE001
            pass


@router.callback_query(F.data == "mus:cancel")
async def cb_cancel_choice(callback: CallbackQuery, state: FSMContext) -> None:
    """Отмена: обязана быть ВЫШЕ выбора трека — иначе mus:cancel съедает cb_pick_track."""
    await callback.answer()
    await state.clear()
    if callback.message:
        try:
            await callback.message.edit_text("❌ Отменил поиск музыки. Пиши что угодно 🙌")
        except Exception:  # noqa: BLE001
            try:
                await callback.message.delete()
            except Exception:
                pass


@router.callback_query(MusicStates.choosing_source, F.data.startswith("mus:src:"))
async def cb_source_choice(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    db: Database,
) -> None:
    """Выбрали источник: запрос уже есть — сразу к поиску, нет — просим название."""
    data = await state.get_data()
    source = (callback.data or "").split("mus:src:", 1)[-1] or "sc"
    query: str = (data.get("query") or "").strip()

    await callback.answer()
    if source == "vk":
        try:
            configured = await vk_configured(db)
        except Exception as exc:  # noqa: BLE001 - база не отвечает и т.п.
            log.exception("проверка VK-привязки не удалась")
            await callback.answer(
                f"Не удалось проверить привязку ВК ({type(exc).__name__}) — "
                "попробуй ещё раз через минуту.",
                show_alert=True,
            )
            return
        if not configured:
            await callback.answer(
                "ВКонтакте ещё не привязан. Владелец может сделать это без "
                "пароля: команда /vklogin — открыть ссылку в браузере, "
                "подтвердить вход и прислать боту адрес страницы.",
                show_alert=True,
            )
            return

    if not query:
        await state.update_data(source=source)
        await state.set_state(MusicStates.waiting_query)
        if callback.message:
            await callback.message.edit_text(ASK_NAME)
        return

    # Название уже есть (например, из обычного текста «скачай песню …») — ищем сразу.
    await state.update_data(source=source)
    if callback.message:
        try:
            await callback.message.delete()
        except Exception:  # noqa: BLE001
            pass
    await _show_choices(callback.message, query, state, settings, db=db)


@router.callback_query(MusicStates.choosing, F.data.startswith("mus:"))
async def cb_pick_track(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    settings: Settings,
) -> None:
    data = await state.get_data()
    tracks: list[dict] = data.get("tracks") or []
    query: str = data.get("query") or ""
    source: str = data.get("source") or "sc"

    try:
        index = int((callback.data or "").split(":", 1)[1])
    except (IndexError, ValueError):
        index = -1
    if index < 0 or index >= len(tracks):
        await state.clear()
        await callback.answer("Выбор устарел — напиши трек ещё раз 🙂", show_alert=True)
        return

    await state.clear()

    track = Track(
        title=tracks[index].get("title", "Без названия"),
        performer=tracks[index].get("performer", "Unknown source"),
        url=tracks[index].get("url", ""),
        duration=int(tracks[index].get("duration", 0) or 0),
    )
    if not track.url:
        await callback.answer("Не получилось: трек без ссылки. Попробуй ещё раз.")
        return

    chat = await ensure_active_chat(db, callback.from_user.id, settings)
    await callback.answer("Скачиваю…")
    await _deliver(
        callback.message, chat, track, query or track.title, db, settings, source
    )


# --- Перехват ссылок ТикТока в обычном тексте —--------------------------

def _has_tiktok_link(message: Message) -> bool:
    """Фильтр: в тексте есть ссылка на ТикТок (иначе сообщение не наше)."""
    return bool(video_in_url(message.text))


@router.message(_has_tiktok_link)
async def tiktok_message(
    message: Message,
    state: FSMContext,
    db: Database,
    settings: Settings,
) -> None:
    """Ссылка на ТикТок в ЛЮБОМ тексте — качаем видео без водяного знака."""
    url = video_in_url(message.text) or ""
    await state.clear()

    placeholders: Message = await message.answer(TIKTOK_DOWNLOADING)
    chat = await ensure_active_chat(db, message.from_user.id, settings)
    try:
        data, filename, caption = await download_video(url)
    except TikTokError as exc:
        text = f"🎬 {exc}"
        try:
            await placeholders.edit_text(text)
        except TelegramBadRequest:
            await message.answer(text)
        await db.touch_chat(chat.id)
        return

    try:
        await placeholders.delete()
    except Exception:  # noqa: BLE001
        pass

    try:
        await message.answer_video(BufferedInputFile(data, filename=filename), caption=caption)
    except Exception as exc:  # noqa: BLE001 - Telegram не принял как видео
        log.warning("Не удалось отправить видео (%s) — шлю файлом", exc)
        try:
            await message.answer_document(
                BufferedInputFile(data, filename=filename), caption=caption
            )
        except Exception:
            log.exception("Не удалось отправить видео")
            await message.answer("😔 Видео скачалось, но Telegram его не принял.")
            await db.touch_chat(chat.id)
            return

    await db.add_message(chat.id, ROLE_USER, f"🎬 ТикТок по ссылке: {url}")
    await db.add_message(chat.id, ROLE_ASSISTANT, f"🎬 Отправил видео: {caption}")
    await db.touch_chat(chat.id)


# --- YouTube: ссылка → «смотрю» видео и запоминаю в чате ------------------

YOUTUBE_READING = "🎬 Смотрю видео с YouTube — читаю название, описание и субтитры..."


def _has_youtube_link(message: Message) -> bool:
    """Фильтр: в тексте есть ссылка на YouTube (обычную, Shorts или youtu.be)."""
    text = (message.text or "").strip()
    if text.startswith("/"):
        return False  # команду (/video ...) не перебиваем
    return bool(youtube_in_url(text))


@router.message(_has_youtube_link)
async def youtube_message(
    message: Message,
    state: FSMContext,
    db: Database,
    settings: Settings,
    registry,
    locks,
) -> None:
    """Ссылка на YouTube в любом сообщении: читаем видео и отвечаем по нему."""
    url = youtube_in_url(message.text) or ""
    await state.clear()
    chat = await ensure_active_chat(db, message.from_user.id, settings)
    placeholder: Message = await message.answer(YOUTUBE_READING)
    try:
        info = await describe_video(url)
    except YouTubeError as exc:
        text = f"🎬 {exc}"
        try:
            await placeholder.edit_text(text)
        except TelegramBadRequest:
            await message.answer(text)
        await db.touch_chat(chat.id)
        return
    except Exception:  # noqa: BLE001 - сеть/yt-dlp
        log.exception("YouTube: не удалось прочитать видео")
        try:
            await placeholder.edit_text(
                "🎬 Не смог прочитать это видео. Проверь ссылку и попробуй ещё раз."
            )
        except TelegramBadRequest:
            pass
        await db.touch_chat(chat.id)
        return

    try:
        await placeholder.delete()
    except Exception:  # noqa: BLE001
        pass

    # Вопрос человека без ссылки; если он просто кинул ссылку — просим пересказ.
    question = question_from_text(message.text, url)
    from .chat import _locked  # локально: chat.py не импортирует services

    await _locked(
        message,
        f"{question}\n\n🎬 YouTube: {info.url}",
        db,
        settings,
        registry,
        locks,
        context=info.context,
    )


# --- Перехват просьбы о музыке в ОБЫЧНОМ тексте (без Сервисов) —---------

_MUS_ACTION_RE = re.compile(
    r"\b(скач\w*|загруз\w*|включ\w*|запуст\w*|постав\w*|подбер\w*|"
    r"найди\b|спой\w*|download|search for|play)\b",
    re.I,
)
_MUS_SUBJECT_RE = re.compile(
    r"\b(музык\w*|песн\w*|трек\w*|композицию|композици\w*|звук\w*|song|track)\b",
    re.I,
)
_MUS_NOISE_RE = re.compile(r"\b(мне|пожалуйста|плиз|плз|сейчас|quickly|please)\b", re.I)
_MUS_QUESTION_RE = re.compile(
    r"\b(умеешь|можешь|сможешь|какая|какую|какой|зачем|откуда|локация)\b", re.I
)


def _music_query_from_text(text: str | None) -> str | None:
    """Из обычного текста достаём запрос на музыку (или None, если это не она).

    «скачай песню иван дорн — стыцамэн» → «иван дорн — стыцамэн»
    «включи музыку killa hota» → «killa hota»
    «умеешь скачивать музыку?» → None (это вопрос, на него отвечает модель).
    «поставь трек без названия» → «» (действие есть, названия нет).
    Ссылка на ТикТок важнее — её перехватит тикток-обработчик.
    """
    if not text:
        return None
    stripped = text.strip()
    if "tiktok.com" in stripped.lower():
        return None
    if _MUS_QUESTION_RE.search(stripped):
        return None
    if not _MUS_ACTION_RE.search(stripped):
        return None
    if not _MUS_SUBJECT_RE.search(stripped):
        return None
    cleaned = _MUS_ACTION_RE.sub(" ", stripped)
    cleaned = _MUS_SUBJECT_RE.sub(" ", cleaned)
    cleaned = _MUS_NOISE_RE.sub(" ", cleaned)
    # Убираем закорючки по краям: кавычки, тире, знаки препинания.
    cleaned = cleaned.strip(" \t\r\n,.-!?«»\"';:")
    query = " ".join(cleaned.split())
    if len(query) < 2:
        return ""
    return query


def _is_music_request(message: Message) -> bool:
    """Фильтр: это просьба о музыке (в обычном тексте, без Сервисов)."""
    return _music_query_from_text(message.text) is not None


@router.message(_is_music_request)
async def music_casual(
    message: Message,
    state: FSMContext,
    settings: Settings,
) -> None:
    """Просто текстом: «скачай песню …» → предлагаем источник → ищем."""
    query = _music_query_from_text(message.text) or ""
    await state.clear()
    await state.set_state(MusicStates.choosing_source)
    await state.update_data(query=query)

    if not query:
        await message.answer(
            "🎵 Ок, найду трек — но надо имя: напиши исполнителя или "
            "название (например: «скачай песню Smash все это время»)."
        )
        return

    await message.answer(
        f"🎵 «{query}» — где качаем?",
        reply_markup=music_source_keyboard(),
    )


@router.message(MusicStates.waiting_query, F.text)
async def music_query(
    message: Message,
    state: FSMContext,
    db: Database,
    settings: Settings,
) -> None:
    """Источник выбран, ждем название трека; текст — это и есть название."""
    text = (message.text or "").strip()
    # ВНИМАНИЕ: источник вытащить ДО clear() — иначе потеряем «VK» и поиск
    # молча уедет в SoundCloud (с SC такое было незаметно, он дефолт).
    data = await state.get_data()
    source: str = data.get("source") or "sc"
    await state.clear()

    # Пока бот ждёт название трека, человек мог нажать другие кнопки —
    # тогда выполняем именно их, а не интерпретируем как запрос.
    from .chats import cmd_chats, cmd_new
    from .settings import cmd_settings  # локально, чтобы избежать циклов

    if text == BTN_NEW:
        await cmd_new(message, db, settings)
        return
    if text == BTN_CHATS:
        await cmd_chats(message, db, settings)
        return
    if text == BTN_SETTINGS:
        await cmd_settings(message, db, settings)
        return
    if text.startswith("/"):
        await message.answer("Ок, отменяю поиск музыки. Что нужно — пиши 🙂")
        return

    await state.update_data(source=source)
    await _show_choices(message, text, state, settings, db=db)
