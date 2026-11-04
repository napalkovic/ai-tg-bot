"""Оповещения о БПЛА: пересылка новых постов канала подписчикам бота.

Модель (как просил владелец):
- канал ОБЩИЙ: по умолчанию @Lipetsk_radar; любой пользователь может
  сменить его секретной командой /uav (ссылка применяется для всех);
- а включение/выключение — ЛИЧНОЕ: каждый сам решает, слать ли ему
  посты (кнопка в Настройках или /uavon · /uavoff);
- по умолчанию оповещения ВЫКЛЮЧЕНЫ.

Уровень БПЛА (просьба владельца 4 ноября 2026):
- из постов канала распознаётся ТЕКУЩИЙ уровень (🔴 красный / 🟡 жёлтый /
  🟢 зелёный) и хранится в kv; при смене уровня НИКАКОГО отдельного
  сообщения «уровень изменился» нет — владелец просил просто пересылать
  сам пост канала (это делается ниже, как обычно);
- включённым пользователям уровень подмешивается в контекст ответов ИИ
  («ИИ знает уровень»), а на прямые вопросы «какой уровень БПЛА / тревоги»
  отвечает точно и мгновенно (без похода в модель);
- у кого оповещения ВЫКЛЮЧЕНЫ — тому ИИ про уровни ничего не знает и
  на такие вопросы честно отвечает «не знаю».

Как берётся канал без админки: у каждого ПУБЛИЧНОГО канала (t.me/имя)
есть веб-страница t.me/s/имя — её отдаёт любой браузер без входа.
Когда у кого-то включены оповещения, бот каждые 15 СЕКУНД читает страницу
и рассылает новые посты всем «включившимся»: текст + фото, ниже кнопка
«🔕 Отключить оповещения» (текстом команду не дублируем — команда /uavoff).

Честные ограничения:
- работают только ПУБЛИЧНЫЕ каналы (закрытые t.me/+хэш не читаются);
- задержка до ~15–20 секунд (страница опрашивается);
- видео файлом не пересылается — бот сообщает, что в посте видео.
"""

from __future__ import annotations

import asyncio
import html as html_module
import logging
import random
import re
import time
import urllib.request
from dataclasses import dataclass

from aiogram import Bot
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from .db import Database

log = logging.getLogger(__name__)

DEFAULT_CHANNEL = "Lipetsk_radar"   # общий канал, если через /uav не задали свой
KV_CHANNEL = "uav_channel"          # имя канала (slug) — общее для всех
KV_LAST = "uav_last"                # номер последнего отправленного поста
USER_PREFIX = "uav_user:"           # uav_user:<id> = "1"/"0" — личный выключатель

# Текущий уровень БПЛА (общий, вычисляется из постов канала).
KV_LEVEL = "uav_level"              # "red" | "yellow" | "green"
KV_LEVEL_TEXT = "uav_level_text"    # фраза из поста (для справки)
KV_LEVEL_POST = "uav_level_post"    # номер поста, откуда взяли уровень
KV_LEVEL_AT = "uav_level_at"        # когда заметили (unix-время)

LEVEL_ORDER = {"green": 0, "yellow": 1, "red": 2}

# Явные эмодзи уровня важнее слов: «🟡 Угроза атаки» — это жёлтый, не красный.
LEVEL_EMOJI_GREEN = "🟢"
LEVEL_EMOJI_YELLOW = "🟡"
LEVEL_EMOJI_RED = "🔴"


def user_key(user_id: int) -> str:
    return f"{USER_PREFIX}{user_id}"


POLL_INTERVAL = 15.0         # раз в 15 секунд — наименьшая разумная пауза
BACKOFF_MAX = 120.0          # при сбоях растём до 2 минут, сайт не дёргаем
FETCH_TIMEOUT = 20.0
MAX_POSTS_PER_CYCLE = 5      # не заваливаем после долгого простоя
MAX_TEXT_CHARS = 3500

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)

# Подсказка «/uavoff» текстом убрана — под сообщением и так есть кнопка.

_BUTTON_UAV_OFF = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🔕 Отключить оповещения о БПЛА",
                callback_data="uav:off",
            )
        ],
    ]
)


@dataclass
class UavPost:
    """Разобранный пост веб-версии канала."""

    id: int
    text: str = ""
    images: list[str] | None = None
    has_video: bool = False

    def __post_init__(self) -> None:
        if self.images is None:
            self.images = []


# --- уровень БПЛА ---------------------------------------------------------


LEVEL_LABELS = {"green": "Зелёный", "yellow": "Жёлтый", "red": "Красный"}
LEVEL_EMOJI = {
    "green": LEVEL_EMOJI_GREEN,
    "yellow": LEVEL_EMOJI_YELLOW,
    "red": LEVEL_EMOJI_RED,
}


@dataclass
class UavLevel:
    """Текущий распознанный уровень."""

    code: str
    text: str = ""
    post_id: int = 0
    at: float = 0.0

    @property
    def emoji(self) -> str:
        return LEVEL_EMOJI.get(self.code, "⚪")

    @property
    def label(self) -> str:
        return LEVEL_LABELS.get(self.code, "Неизвестный")

    @property
    def title(self) -> str:
        return f"{self.emoji} {self.label}"


def detect_level(text: str | None) -> str | None:
    """Определяем уровень по тексту поста канала (или None, если это не про уровень).

    Порядок важен: «Отбой опасности атаки» — это зелёный, хотя содержит слово
    «опасность». Поэтому сначала явные эмодзи, затем «отбой/зелёный», и только
    потом красные и жёлтые слова.
    """
    if not text:
        return None
    raw = text.strip()
    if not raw:
        return None

    if LEVEL_EMOJI_GREEN in raw:
        return "green"
    if LEVEL_EMOJI_RED in raw:
        return "red"
    if LEVEL_EMOJI_YELLOW in raw:
        return "yellow"

    if re.search(r"(?i)отбой|миновал\w*|зел[её]н", raw):
        return "green"
    if re.search(r"(?i)красн\w*\s+уровен\w*|уровен\w*\s*:?\s*красн", raw):
        return "red"
    if re.search(r"(?i)опасност\w*|атак\w*|тревог\w*|прилёт|прилет", raw):
        return "red"
    # «БПЛА» само по себе — только если это не сводка/статистика о сбитых.
    if re.search(r"(?i)\bбпла\b|\bдрон\w*", raw) and not re.search(
        r"(?i)сбит\w*|перехват\w*|уничтож\w*|сводк\w*|статистик\w*|итог\w*",
        raw,
    ):
        return "red"
    if re.search(r"(?i)ж[её]лт\w*|повышенн\w*|угроз\w*", raw):
        return "yellow"
    return None


def latest_level(posts: list[UavPost]) -> UavLevel | None:
    """Берём уровень из САМОГО СВЕЖЕГО поста, где он вообще упоминается.

    Если после этого поста были обычные новости без уровня — уровень не
    сбрасывается: он «держится» до следующего поста с уровнем.
    """
    for post in sorted(posts, key=lambda item: item.id, reverse=True):
        code = detect_level(post.text)
        if code is None:
            continue
        return UavLevel(
            code=code,
            text=(post.text or "").strip()[:500],
            post_id=post.id,
            at=time.time(),
        )
    return None


async def get_level(db: Database) -> UavLevel | None:
    """Текущий уровень из kv (None, если ещё ни разу не распознали)."""
    code = (await db.kv_get(KV_LEVEL) or "").strip().lower()
    if code not in LEVEL_ORDER:
        return None
    try:
        post_id = int((await db.kv_get(KV_LEVEL_POST)) or 0)
    except ValueError:
        post_id = 0
    try:
        at = float((await db.kv_get(KV_LEVEL_AT)) or 0)
    except ValueError:
        at = 0.0
    return UavLevel(
        code=code,
        text=(await db.kv_get(KV_LEVEL_TEXT) or "").strip(),
        post_id=post_id,
        at=at,
    )


async def save_level(db: Database, level: UavLevel) -> None:
    await db.kv_set(KV_LEVEL, level.code)
    await db.kv_set(KV_LEVEL_TEXT, level.text[:500])
    await db.kv_set(KV_LEVEL_POST, str(level.post_id))
    await db.kv_set(KV_LEVEL_AT, str(level.at))


async def reset_level(db: Database) -> None:
    """Забыть уровень (например, при смене канала)."""
    for key in (KV_LEVEL, KV_LEVEL_TEXT, KV_LEVEL_POST, KV_LEVEL_AT):
        try:
            await db.kv_delete(key)
        except Exception:  # noqa: BLE001
            log.exception("UAV: не удалил ключ уровня %s", key)


def _ago(at: float) -> str:
    """« (5 минут назад)» — человеческая подпись к уровню."""
    if not at:
        return ""
    seconds = max(0, int(time.time() - at))
    if seconds < 90:
        return " (только что)"
    minutes = seconds // 60
    if minutes < 60:
        return f" ({minutes} мин назад)"
    hours = minutes // 60
    if hours < 24:
        return f" ({hours} ч назад)"
    return f" ({hours // 24} дн назад)"


def level_line(level: UavLevel | None) -> str:
    if level is None:
        return "нет данных"
    return f"{level.title}{_ago(level.at)}"


async def update_level(
    db: Database, posts: list[UavPost]
) -> tuple[UavLevel | None, UavLevel | None]:
    """Обновляем уровень из постов. Возвращаем (старый, новый).

    Новый = None, если ничего не поменялось: не пишем в базу каждые 15 секунд
    (Neon не любит лишние записи) и не трогаем время обновления.
    """
    fresh = latest_level(posts)
    if fresh is None:
        return await get_level(db), None
    old = await get_level(db)
    if (
        old is not None
        and old.code == fresh.code
        and old.post_id == fresh.post_id
    ):
        return old, None
    await save_level(db, fresh)
    return old, fresh


# --- вопросы про уровень (для ИИ) ----------------------------------------

# Вопросы, на которые бот отвечает сам, точно и мгновенно. Специально
# НЕ ловим просто «бпла» — иначе «что такое бпла» получило бы ответ про
# сегодняшний уровень.
_LEVEL_QUESTION_RE = re.compile(
    r"(?i)("
    r"уровен\w*[^.!?]{0,40}(?:бпла|тревог|опасн|угроз|атак|красн|зел[её]н|ж[её]лт)|"
    r"(?:красн|зел[её]н|ж[её]лт)\w*\s+уровен\w*|"
    r"уровен\w*\s*(?:бпла|тревог|опасн|угроз|атак|красн|зел[её]н|ж[её]лт)|"
    r"тревог\w*\s*(?:бпла|сейчас|есть|объявлен|включен|включён)|"
    r"(?:есть|нет|был[аи]?|объявлен\w*)\s+(?:ли\s+)?(?:опасност|угроз|тревог|красн)\w*|"
    r"опасност\w*\s+атак\w*|атак\w*\s+бпла|"
    r"\bотбой\b|"
    r"что\s+с\s+(?:бпла|дронам\w*)|"
    r"бпла\s+(?:сейчас|летят|летают|над)"
    r")"
)

_LEVEL_EXACT = {
    "уровень",
    "какой уровень",
    "какой сейчас уровень",
    "уровень сейчас",
    "уровень опасности",
    "уровень тревоги",
    "уровень бпла",
    "уровень атаки",
    "тревога",
    "отбой",
    "опасность",
}


def is_level_question(text: str | None) -> bool:
    """Похоже ли сообщение на вопрос про уровень БПЛА/тревоги."""
    if not text:
        return False
    stripped = " ".join(text.strip().split())
    low = stripped.lower().strip(" ?!.…")
    if low in _LEVEL_EXACT:
        return True
    return bool(_LEVEL_QUESTION_RE.search(stripped))


async def uav_enabled(db: Database, user_id: int) -> bool:
    return (await db.kv_get(user_key(user_id))) == "1"


async def ai_level_note(
    db: Database, user_id: int, *, enabled: bool | None = None
) -> str:
    """Вставка про уровень для промпта — только если у человека включено.

    Выключил оповещения → ИИ вообще не получает никаких сведений об уровне
    (просьба владельца: «если отключаю — ИИ не должен знать об уровнях»).
    `enabled` можно передать заранее — чтобы не читать флажок из базы дважды.
    """
    if enabled is None:
        enabled = await uav_enabled(db, user_id)
    if not enabled:
        return ""
    level = await get_level(db)
    if level is None:
        return (
            "[Оповещения о БПЛА включены, но сообщений об уровне пока не "
            "приходило. Если спросят про уровень тревоги или БПЛА — честно "
            "скажи, что данных пока нет.]"
        )
    note = (
        "[Оповещения о БПЛА включены. Текущий уровень опасности БПЛА в "
        f"регионе: {level.title}{_ago(level.at)}. "
    )
    if level.text:
        short = " ".join(level.text.split())[:300]
        note += f"Последнее сообщение канала: {short}. "
    note += (
        "Если спрашивают про уровень тревоги, БПЛА, опасность атаки или "
        "«красный/зелёный уровень» — отвечай именно этим. Другие уровни "
        "не выдумывай. Это САМОЕ СВЕЖЕЕ значение: если в истории чата раньше "
        "упоминался другой уровень, верен этот.]"
    )
    return note


async def answer_level_question(db: Database, user_id: int) -> str:
    """Точный ответ на прямой вопрос про уровень (без обращения к модели)."""
    if not await uav_enabled(db, user_id):
        return (
            "🔕 У тебя выключены оповещения о БПЛА — про уровни я ничего не "
            "знаю и не отслеживаю.\nВключить: /uavon (или кнопка в "
            "«⚙️ Настройках»)."
        )
    level = await get_level(db)
    if level is None:
        return (
            "📡 Оповещения включены, но сообщений об уровне в канале пока не "
            "было. Как только появится — сразу скажу."
        )
    text = f"Сейчас уровень БПЛА: {level.title}{_ago(level.at)}."
    if level.text:
        short = " ".join(level.text.split())[:300]
        text += f"\n\nПоследнее сообщение канала:\n{short}"
    return text


# --- чтение канала --------------------------------------------------------


def channel_slug(text: str | None) -> str | None:
    """«https://t.me/s/name», «t.me/name», «@name», «name» → «name» (или None).

    Закрытые каналы (t.me/+хэш) не принимаются: веб-версии у них нет.
    """
    raw = " ".join((text or "").split())
    raw = re.sub(r"^https?://", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"^t\.me/", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"^s/", "", raw, flags=re.IGNORECASE)
    raw = raw.lstrip("@").split("?")[0].split("#")[0].strip("/")
    if not raw:
        return None
    if raw.startswith("+"):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_]{3,64}", raw):
        return None
    return raw


def _strip_tags(fragment: str) -> str:
    """HTML поста → почти обычный текст (переводы строк и сущности сохраняем).

    Ссылки (<a>...</a>) вырезаем целиком вместе с текстом и подписываем:
    канал в «шапке» каждого поста публикует кнопки-ссылки («Оповещения»,
    «Дать нам голоса», «Обратная связь...»), а владелец просил пересылать
    только саму информацию. Строки, в которых после вырезания ссылок не
    осталось ни одной буквы, выбрасываем целиком.
    """
    fragment = re.sub(r"<br\s*/?>", "\n", fragment)
    fragment = re.sub(r"<a\b[^>]*>[\s\S]*?</a>", "", fragment)
    fragment = re.sub(r"<[^>]+>", "", fragment)
    text = html_module.unescape(fragment)
    lines = []
    for line in text.split("\n"):
        line = line.rstrip()
        if not re.search(r"[A-Za-zА-Яа-яЁё0-9]", line):
            # Только эмодзи, палочки «|» и точки — мусор от вырезанных ссылок.
            continue
        lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_posts(page_html: str) -> list[UavPost]:
    """Разбираем страницу t.me/s/<канал>: посты, текст, фото, видео."""
    posts: list[UavPost] = []
    for chunk in re.split(r'data-post="', page_html)[1:]:
        head = re.match(r"[^\"/]+/(\d+)", chunk)
        if not head:
            continue
        post = UavPost(id=int(head.group(1)))

        text_match = re.search(
            r'class="tgme_widget_message_text[^"]*"[^>]*>([\s\S]*?)</div>', chunk
        )
        if text_match:
            post.text = _strip_tags(text_match.group(1))

        # Ссылки на фото бывают в разных экранированиях: url('...') и url(&#39;...&#39;).
        for first, second in re.findall(
            r"background-image:url\('([^']+)'|background-image:url\(&#39;([^&]+)&#39;",
            chunk,
        ):
            image = html_module.unescape(first or second)
            if "emoji/" in image:
                continue
            if image.startswith("http"):
                post.images.append(image)

        post.has_video = 'class="tgme_widget_message_video' in chunk
        posts.append(post)

    # В HTML посты идут от новых к старым — выравниваем по возрастанию id.
    unique = {post.id: post for post in posts}
    return [unique[key] for key in sorted(unique)]


def fetch_posts(slug: str) -> list[UavPost]:
    """Загружаем веб-версию канала (синхронно — вызывать из потока)."""
    url = f"https://t.me/s/{slug}"
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
        return parse_posts(response.read().decode("utf-8", "replace"))


async def fetch_posts_async(slug: str) -> list[UavPost]:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(fetch_posts, slug), timeout=FETCH_TIMEOUT + 5
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"канал прочитать не вышло ({type(exc).__name__})") from exc


async def skip_backlog(db: Database) -> None:
    """Поставить планку на САМЫЙ СВЕЖИЙ пост канала (просрочку не пересылаем).

    Нужен при включении оповещений: пока они были выключены, в канале могли
    выйти посты — владелец просил их НЕ присылать после повторного
    включения (баг замечен 4 ноября 2026). Если прочитать канал сейчас
    не выйдет, планку не трогаем — тогда сработает ограничение poll_once
    (максимум 5 самых свежих).
    """
    slug = await db.kv_get(KV_CHANNEL) or DEFAULT_CHANNEL
    try:
        posts = await fetch_posts_async(slug)
    except Exception:  # noqa: BLE001 — канал мог моргнуть, не критично
        return
    if posts:
        await db.kv_set(KV_LAST, str(max(post.id for post in posts)))


def _post_body(post: UavPost) -> str:
    parts: list[str] = []
    if post.text:
        parts.append(post.text[:MAX_TEXT_CHARS])
    if post.has_video:
        parts.append("🎬 В этом посте есть видео — смотреть в канале.")
    return "\n".join(parts)


async def _send_image(bot: Bot, user_id: int, post: UavPost) -> bool:
    """Одно фото за пост достаточно. Возвращает True, если отправилось."""
    if not post.images:
        return False
    image_url = post.images[0]
    caption = _post_body(post)[:1000] if post.text else None
    try:
        await bot.send_photo(
            user_id, image_url, caption=caption, reply_markup=_BUTTON_UAV_OFF
        )
        return True
    except Exception as exc:  # noqa: BLE001 - ссылку Telegram может не съесть
        log.info("Фото по ссылке не отправилось (%s) — качаю сам", exc)
    try:
        request = urllib.request.Request(image_url, headers={"User-Agent": UA})
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
            data = response.read()
        await bot.send_photo(
            user_id,
            BufferedInputFile(data, filename="post.jpg"),
            caption=caption,
            reply_markup=_BUTTON_UAV_OFF,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("Фото не отправилось вовсе: %s", exc)
        return False


async def send_post(bot: Bot, user_id: int, post: UavPost) -> None:
    body = _post_body(post)
    if not (body or post.images):
        return  # вырезали только «шапку» со ссылками — пересылать нечего
    photo_sent = await _send_image(bot, user_id, post)
    if photo_sent and body and len(post.text or "") <= 1000:
        return  # текст уже ушёл подписью к фото
    try:
        # Подсказку «/uavoff» текстом убрали по просьбе владельца:
        # под сообщением висит кнопка «🔕 Отключить оповещения».
        await bot.send_message(user_id, body, reply_markup=_BUTTON_UAV_OFF)
    except Exception:  # noqa: BLE001
        log.exception("Пост %s не отправился пользователю %s", post.id, user_id)


async def poll_once(bot: Bot, db: Database) -> bool:
    """Один цикл: получить новые посты общего канала и разослать включившимся.

    Возвращает False, если канал прочитать не удалось (петля растянет
    следующую паузу — не молотим по упавшему сайту).
    """
    flags = await db.kv_prefix(USER_PREFIX)
    recipients = [
        int(key.split(":", 1)[1])
        for key, value in flags.items()
        if value == "1" and key.split(":", 1)[1].isdigit()
    ]
    if not recipients:
        return True  # никто не подписан — сеть не дёргаем
    slug = await db.kv_get(KV_CHANNEL) or DEFAULT_CHANNEL

    try:
        posts = await fetch_posts_async(slug)
    except RuntimeError as exc:
        log.warning("UAV канал %s: %s", slug, exc)
        return False
    if not posts:
        log.info("UAV канал %s: постов не видно", slug)
        return False

    # Уровень обновляем ВСЕГДА, даже если новых постов нет: он «держится»
    # до следующего поста с уровнем. Отдельного сообщения «уровень изменился»
    # НЕТ (просьба владельца): подписчикам уходит просто сам пост канала.
    try:
        await update_level(db, posts)
    except Exception:  # noqa: BLE001 - уровень не должен ронять рассылку
        log.exception("UAV: не обновил уровень")

    last_raw = int((await db.kv_get(KV_LAST)) or 0)
    if last_raw <= 0:
        # Первый запуск (или канал сменили): фиксируем планку, старые не сыпем.
        await db.kv_set(
            KV_LAST, str(max(post.id for post in posts))
        )
        return True

    fresh = [post for post in posts if post.id > last_raw]
    if not fresh:
        return True
    if len(fresh) > MAX_POSTS_PER_CYCLE:
        fresh = fresh[-MAX_POSTS_PER_CYCLE:]

    for post in fresh:
        for user_id in recipients:
            await send_post(bot, user_id, post)
    await db.kv_set(KV_LAST, str(fresh[-1].id))
    log.info("UAV: разослал %d постов %d пользователям", len(fresh), len(recipients))
    return True


async def uav_poller(bot: Bot, db: Database) -> None:
    """Фоновая петля: проверяем канал каждые 15 с (только если есть слушатели).

    При устойчивых сбоях пауза растёт в два раза — до двух минут — и
    восстанавливается обратно после первого же успеха.
    """
    log.info("Оповещения о БПЛА: наблюдатель запущен (интервал %.0f с)", POLL_INTERVAL)
    await asyncio.sleep(3)  # даём боту разогнаться
    delay = POLL_INTERVAL
    while True:
        try:
            if await poll_once(bot, db):
                delay = POLL_INTERVAL
            else:
                delay = min(delay * 2, BACKOFF_MAX)
        except Exception:  # noqa: BLE001 - наблюдатель не должен умирать
            log.exception("UAV: сбой в цикле оповещений")
            delay = min(delay * 2, BACKOFF_MAX)
        await asyncio.sleep(delay + random.uniform(0, 1.5))
