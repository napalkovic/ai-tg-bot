"""ВКонтакте: поиск и скачивание музыки по «мобильному» токену (audio API).

История: у части треков SoundCloud честно лежит 30-секундный отрывок
(«сниппеты» коммерческой музыки), даже с токеном аккаунта. Во ВКонтакте
почти вся музыка отдаётся целиком — это второй источник для сложных треков.

Важно (2016-2026): VK закрыл audio.* методы для всех «браузерных» токенов
— и для привязки /vklogin, и для любых OAuth (весна 2026, см. issue 547
в vk_api: у всех «Unknown method passed»). Работает только вход, который
делает официальное мобильное приложение: логин + пароль через
Kate Mobile (client_id 2685278 и его публичный client_secret).
Это делаем библиотекой vkpymusic — она повторяет мобильный вход и держит
свой транспорт (curl_cffi с TLS-отпечатком Chrome, иначе CDN VK режет
обычные requests/urllib).

Настройка (по убыванию приоритета):
1. VK_AUDIO_TOKEN + VK_AUDIO_UA — готовый «мобильный» токен (можно получить
   скриптом scripts/vk_auth.py на ноутбуке, где SMS-код вводишь сам).
2. VK_LOGIN + VK_PASSWORD — бот сам сделает вход; если VK попросит SMS-код
   или капчу, на сервере это не пройти — тогда см. пункт 1.

Токен сохраняется в БД (kv) после первого успешного входа — повторный вход
не нужен, пока VK его не отзовёт.

Технически:
- search_songs_by_text → у Song поле url (прямая ссылка mp3 или hls-плейлист);
  треки без url пропускаем;
- скачивание — curl_cffi (TLS-имитация Chrome): vkuseraudio.net 403-ит
  обычные requests; hls-плейлисты тянет yt-dlp (ffmpeg на борту).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

from .music import MAX_AUDIO_BYTES, MAX_TRACK_SECONDS, MusicError, Track, _safe_name, _clean_seconds

log = logging.getLogger(__name__)

VK_APP_ID_DEFAULT = "2685278"  # Kate Mobile — приложение с scope 'audio'
VK_CLIENT_SECRET = "lxhD8OD7dMsqtXIm5IUY"  # публичный секрет Kate Mobile
DOWNLOAD_TIMEOUT = 240.0
SEARCH_TIMEOUT = 60.0  # VK отвечает быстро; дольше — значит зависло
# Владелец (4 ноября 2026): ровно 10 треков по запросу, без нашей сортировки —
# как VK отдал, так и показываем (только пропускаем треки без прямой ссылки).
TRACKS_LIMIT = 10
VK_TOKEN_KV_KEY = "vk_token"             # токен /vklogin (музыку не открывает)
VK_AUDIO_TOKEN_KV_KEY = "vk_audio_token"  # «мобильный» токен — рабочий
VK_AUDIO_UA_KV_KEY = "vk_audio_ua"        # его user-agent: VK держит пару токен+UA

# Веб-маршрут (основной, без пароля): cookies браузера → долгоживущий токен.
VK_WEB_COOKIES_ENV = "VK_WEB_COOKIES"
VK_WEB_COOKIES_KV_KEY = "vk_web_cookies"
VK_WEB_TOKEN_KV_KEY = "vk_web_token"
VK_WEB_EXPIRES_AT_KV_KEY = "vk_web_expires_at"

WEB_APP_ID = "6287487"  # идентификатор веб-клиента vk.ru — его токены допускают audio.*
WEB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:153.0) "
    "Gecko/20100101 Firefox/153.0"
)

# Кэш «мобильного» токена в процессе (не ходим в БД за каждым поиском).
_audio_token: str | None = None
_audio_ua: str | None = None
_db_token: str | None = None  # токен из БД (vklogin) — читаем один раз


class _VkNotConfigured(Exception):
    """ВКонтакте не настроен (нет логина/пароля в переменных окружения)."""


class _VkAuthFailed(Exception):
    """ВКонтакте не дал «мобильный» токен (капча/2FA/неверный пароль)."""


class _VkTokenDead(Exception):
    """«Мобильный» токен отозван — нужно сбросить кэш и войти заново."""


class _VkWebCookiesStale(Exception):
    """Cookies сессии VK из браузера устарели — нужно обновить скриптом."""


class _VkWebTokenDead(Exception):
    """Веб-токен протух/отозван — свежий создаётся из тех же cookies."""


def _credentials() -> tuple[str, str, str]:
    login = (os.environ.get("VK_LOGIN") or "").strip()
    password = (os.environ.get("VK_PASSWORD") or "").strip()
    app_id = (os.environ.get("VK_APP_ID") or VK_APP_ID_DEFAULT).strip()
    return login, password, app_id


def _audio_env() -> tuple[str, str]:
    """Готовый «мобильный» токен из окружения (если владелец вписал вручную)."""
    token = (os.environ.get("VK_AUDIO_TOKEN") or "").strip()
    ua = _kate_ua()
    return token, ua


def _human_auth_error(exc: Exception) -> str:
    """Превращаем ошибки VK в понятные фразы для чата."""
    text = str(exc).lower()
    if "captcha" in text or "капч" in text:
        return (
            "ВКонтакте просит капчу — слишком частые входы. Открой VK в "
            "приложении или браузере, войди и подтверди, потом попробуй снова."
        )
    if (
        "two-factor" in text or "2fa" in text or "sms" in text
        or "validation" in text or "подтверждение" in text
    ):
        return (
            "У твоего VK двухфакторная защита — на сервере код ввести нечем. "
            "Сделай первый вход на ноутбуке: py scripts/vk_auth.py, токен "
            "вставь в Render."
        )
    if "password" in text or "логин" in text or "incorrect" in text or "invalid client" in text:
        return (
            "ВКонтакте не принял логин/пароль — проверь VK_LOGIN и "
            "VK_PASSWORD в настройках Render."
        )
    if "token" in text and ("invalid" in text or "expired" in text or "revoked" in text):
        return "Токен ВКонтакте больше не действует."
    if "201" in text or "access denied" in text or "permission" in text:
        return "VK не дал доступ к аудио (201 Access denied). Попробуй позже или другой трек."
    if "flood" in text or "too many" in text or "bruteforce" in text:
        return "ВКонтакте просит подождать (слишком часто логинились). Пару минут — и снова."
    snippet = " ".join(str(exc).split())[:140]
    return snippet or "неизвестная ошибка ВКонтакте"


def _app_id() -> str:
    """ID приложения для страницы доступа (/vklogin): Standalone/Kate Mobile."""
    return (
        os.environ.get("VK_OAUTH_APP_ID")
        or os.environ.get("VK_APP_ID")
        or VK_APP_ID_DEFAULT
    ).strip()


def oauth_url() -> str:
    """Ссылка на страницу «Разрешить доступ» — открывается в браузере ноута.

    Начиная с 2026 такой токен музыку НЕ открывает (VK закрыл audio.* для
    oauth) — остаётся полезен только как «запаска»-привязка аккаунта.
    """
    params = {
        "client_id": _app_id(),
        "display": "page",
        "redirect_uri": "https://oauth.vk.com/blank.html",
        "scope": "audio",
        "response_type": "token",
        "v": "5.240",
    }
    return "https://oauth.vk.com/authorize?" + urlencode(params)


def parse_login_result(text: str | None) -> tuple[str, int | None]:
    """Из ответа blank.html достаём токен: …#access_token=…&user_id=….

    Присылается как будто «страница не найдена» — это норма: вся ценность
    в адресе (фрагменте после #), который копирует владелец.
    """
    if not text:
        return "", None
    token_match = re.search(r"access_token=([A-Za-z0-9._-]+)", text)
    if not token_match:
        return "", None
    user_match = re.search(r"user_id=(\d+)", text)
    user_id = int(user_match.group(1)) if user_match else None
    return token_match.group(1), user_id


async def load_token(db=None) -> str | None:
    """Токен привязанного аккаунта (/vklogin): из кэша процесса, потом из БД."""
    global _db_token
    if _db_token:
        return _db_token
    if db is not None:
        value = (await db.kv_get(VK_TOKEN_KV_KEY) or "").strip()
        if value:
            _db_token = value
    return _db_token


def remember_db_token(token: str) -> None:
    """Сразу после сохранения в БД: чтобы не ждать перезапуска процесса."""
    global _db_token
    _db_token = token or _db_token


def reset_session() -> None:
    """Сброс кэша «мобильного» токена (например, после перевставки в БД)."""
    global _audio_token, _audio_ua
    _audio_token = None
    _audio_ua = None


def _kate_ua() -> str:
    """User-agent Kate Mobile — VK держит пару «токен + UA»."""
    try:
        from vkpymusic.client import KateMobileClient

        return KateMobileClient.user_agent
    except Exception:  # noqa: BLE001 — пак нет/другая версия: стандартная строка
        return (
            "KateMobileAndroid/56 lite-460 (Android 4.4.2; SDK 19; x86; "
            "unknown Android SDK built for x86; en)"
        )


def _receive_mobile_token_sync() -> tuple[str, str]:
    """Вход как мобильное приложение VK: логин+пароль → «мобильный» токен.

    Капчу и двухфакторку на сервере не пройти (нечем ввести код) — честно
    сообщаем, что первый вход нужно сделать на ноутбуке (scripts/vk_auth.py).
    """
    login, password, _ = _credentials()
    if not login or not password:
        raise _VkNotConfigured(
            "Для музыки ВКонтакте нужен вход по логину/паролю: добавь "
            "VK_LOGIN и VK_PASSWORD в переменные окружения Render (или "
            "VK_AUDIO_TOKEN из scripts/vk_auth.py). Токен из /vklogin "
            "музыку больше не открывает — VK закрыл поиск для таких токенов."
        )
    try:
        from vkpymusic import TokenReceiver
        from vkpymusic.client import VkMobileClient
    except Exception as exc:  # noqa: BLE001
        raise MusicError(
            "На сервере нет vkpymusic — добавь его в requirements.txt."
        ) from exc

    def on_2fa() -> str:
        raise _VkAuthFailed(
            "VK требует код из SMS/приложения — на сервере ввести его нечем. "
            "Сделай первый вход на ноутбуке: py scripts/vk_auth.py, потом "
            "вставь VK_AUDIO_TOKEN и VK_AUDIO_UA в Render."
        )

    def on_captcha(captcha_url: str) -> str:
        raise _VkAuthFailed(
            "VK показал капчу при входе — на сервере её не пройти. Попробуй "
            "через пару часов, или сделай вход на ноутбуке (scripts/vk_auth.py)."
        )

    def on_invalid_client() -> None:
        raise _VkAuthFailed(
            "VK не принял логин/пароль — проверь VK_LOGIN и VK_PASSWORD "
            "в настройках Render."
        )

    def on_critical_error(response) -> None:
        raise _VkAuthFailed(f"VK не выдал токен: {response}")

    receiver = TokenReceiver(login, password, client=VkMobileClient)
    ok = receiver.auth(
        on_captcha=on_captcha,
        on_2fa=on_2fa,
        on_invalid_client=on_invalid_client,
        on_critical_error=on_critical_error,
    )
    token = receiver.get_token() if ok else None
    if not token:
        raise _VkAuthFailed("VK не выдал токен авторизации.")
    return token, _kate_ua()


async def load_audio_credentials(db=None) -> tuple[str, str] | None:
    """«Мобильный» токен + UA: кэш процесса → БД → окружение → вход по паролю."""
    global _audio_token, _audio_ua
    if _audio_token and _audio_ua:
        return _audio_token, _audio_ua
    if db is not None:
        token = (await db.kv_get(VK_AUDIO_TOKEN_KV_KEY) or "").strip()
        ua = (await db.kv_get(VK_AUDIO_UA_KV_KEY) or "").strip()
        if token and ua:
            _audio_token, _audio_ua = token, ua
            log.info("VK: работаем по «мобильному» токену из БД")
            return token, ua
    env_token, env_ua = _audio_env()
    if env_token and env_ua:
        _audio_token, _audio_ua = env_token, env_ua
        log.info("VK: работаем по «мобильному» токену из окружения")
        return env_token, env_ua
    token, ua = await asyncio.to_thread(_receive_mobile_token_sync)
    _audio_token, _audio_ua = token, ua
    if db is not None:
        await db.kv_set(VK_AUDIO_TOKEN_KV_KEY, token)
        await db.kv_set(VK_AUDIO_UA_KV_KEY, ua)
    log.info("VK: получили «мобильный» токен по логину/паролю")
    return token, ua


def parse_web_cookies(text: str | None) -> dict[str, str] | None:
    """«p=…; remixsid=…» (в любом виде: env, document.cookie, строки) → пара."""
    pairs: dict[str, str] = dict(
        re.findall(r"(?:^|[\s;,])(p|remixsid)=([^;\s]+)", text or "")
    )
    if all(name in pairs for name in ("p", "remixsid")):
        return {"p": pairs["p"], "remixsid": pairs["remixsid"]}
    return None


def _mint_web_token_sync(cookies: dict[str, str]) -> tuple[str, int]:
    """Создать (или обновить) веб-токен из cookies браузера.

    Именно так делает сам веб-плеер vk.ru: POST login.vk.ru/?act=web_token
    с cookies `p` и `remixsid` возвращает выданный vk1.-токен app_id
    6287487, которому открыт доступ к audio.* (живёт десятки дней).
    """
    import requests

    headers = {
        "User-Agent": WEB_UA,
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://vk.ru",
        "Referer": "https://vk.ru/",
        "Cookie": "; ".join(f"{n}={v}" for n, v in cookies.items()),
    }
    try:
        response = requests.post(
            "https://login.vk.ru/?act=web_token",
            headers=headers,
            data={"version": "1", "app_id": WEB_APP_ID},
            timeout=25,
        )
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        raise _VkWebCookiesStale(
            f"VK не отдал токен по cookies ({exc.__class__.__name__})"
        ) from exc
    if payload.get("type") != "okay":
        info = str(payload.get("error_info") or payload)[:200]
        if "unauthorized" in info.lower():
            raise _VkWebCookiesStale(
                "VK говорит «unauthorized» — cookies устарели или скопированы не целиком"
            )
        raise _VkWebCookiesStale(f"VK не отдал токен: {info}")
    data = payload.get("data") or {}
    token = str(data.get("access_token") or "")
    if not token:
        raise _VkWebCookiesStale("VK вернул ответ без токена")
    return token, int(data.get("expires") or 0)


async def load_web_credentials(db=None, *, force_refresh: bool = False) -> str | None:
    """Веб-токен (или None, если cookies не настроены).

    Токен кэшируется в БД и обновляется сам: вместо повторного входа
    просто тот же вызов с теми же cookies — это и есть «вход как веб-плеер».
    """
    cookies = parse_web_cookies(os.environ.get(VK_WEB_COOKIES_ENV, ""))
    if cookies is None and db is not None:
        stored = (await db.kv_get(VK_WEB_COOKIES_KV_KEY) or "").strip()
        cookies = parse_web_cookies(stored)
    if not cookies:
        return None

    if not force_refresh and db is not None:
        token = (await db.kv_get(VK_WEB_TOKEN_KV_KEY) or "").strip()
        try:
            expires_at = int((await db.kv_get(VK_WEB_EXPIRES_AT_KV_KEY)) or 0)
        except ValueError:
            expires_at = 0
        # Обновляем заранее (за 10 минут до конца) — VK показывает срок в секундах.
        if token and expires_at > time.time() + 600:
            return token

    token, expires = await asyncio.to_thread(_mint_web_token_sync, cookies)
    expires_at = int(time.time()) + (expires if expires > 60 else 45 * 24 * 3600)
    if db is not None:
        await db.kv_set(VK_WEB_TOKEN_KV_KEY, token)
        await db.kv_set(VK_WEB_EXPIRES_AT_KV_KEY, str(expires_at))
    log.info("VK: веб-токен получен из cookies (истекает через %s с)", expires or "?")
    return token


def _web_search_sync(query: str, limit: int, token: str) -> list[dict]:
    """audio.search с веб-токеном — возвращаем сырые items VK."""
    import requests

    params = {
        "v": "5.282",
        "access_token": token,
        "q": query,
        # Берём с запасом: часть отсеется в _track_from (треки без прямой
        # ссылки), а владельцу нужно ровно 10 треков в порядке выдачи VK.
        # «sort» не передаём: наша сортировка/популярность не нужна.
        "count": str(min(max(limit * 3, 30), 300)),
        "auto_complete": "1",
    }
    last_error: Exception | None = None
    for host in ("api.vk.ru", "api.vk.com"):
        try:
            response = requests.get(
                f"https://{host}/method/audio.search", params=params, timeout=30
            )
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            log.info("VK: поиск через %s не вышел: %s", host, exc)
            continue
        error = (data or {}).get("error")
        if error:
            code = int(error.get("error_code") or 0)
            if code == 5:
                raise _VkWebTokenDead(str(error.get("error_msg") or "auth failed"))
            raise MusicError(
                f"ВКонтакте не дал поиск: {_human_auth_error(str(error))}"
            )
        items = ((data or {}).get("response") or {}).get("items")
        return list(items or [])
    raise MusicError(
        f"ВКонтакте не ответил: {_human_auth_error(last_error or RuntimeError('нет связи'))}"
    )


def _track_from(item: dict) -> Track | None:
    """Трек VK (web/mobile payload): должно быть url, вразумительная длина."""
    if not isinstance(item, dict):
        return None
    url = item.get("url") or ""
    if not url:
        return None
    seconds = _clean_seconds(item.get("duration"))
    if seconds <= 0 or seconds > MAX_TRACK_SECONDS:
        return None
    title = (item.get("title") or "Без названия").strip()
    performer = (item.get("artist") or "").strip()
    if not performer:
        performer = "ВКонтакте"
    return Track(title=title, performer=performer, url=url, duration=seconds)


def _mobile_search_sync(
    query: str, limit: int, token: str, ua: str
) -> list[Track]:
    """Поиск через vkpymusic (синхронный Service — вызывать из потока)."""
    from vkpymusic import Service

    service = Service(ua, token)
    try:
        songs = service.search_songs_by_text(
            query, count=min(max(limit * 3, 30), 300), offset=0
        )
    except _VkTokenDead:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("VK: поиск не удался: %s", exc)
        text = str(exc).lower()
        if (
            "invalid access_token" in text
            or "token is invalid" in text
            or "expired" in text
            or "error 8" in text
        ):
            raise _VkTokenDead(str(exc)) from exc
        raise MusicError(f"ВКонтакте не дал поиск: {_human_auth_error(exc)}") from exc

    tracks: list[Track] = []
    for song in songs:
        track = _track_from_song(song)
        if track is None:
            continue
        tracks.append(track)
        if len(tracks) >= max(1, limit):
            break
    return tracks


def _track_from_song(song) -> Track | None:
    """Трек VK: должно быть url, вразумительная длина."""
    url = (getattr(song, "url", "") or "").strip()
    if not url:
        # Часть треков VK не отдаёт прямую ссылку (защита) — пропускаем.
        return None
    try:
        seconds = _clean_seconds(int(getattr(song, "duration", 0) or 0))
    except (TypeError, ValueError):
        seconds = 0
    if seconds <= 0 or seconds > MAX_TRACK_SECONDS:
        return None
    title = (getattr(song, "title", "") or "Без названия").strip()
    performer = (getattr(song, "artist", "") or "").strip()
    if not performer:
        performer = "ВКонтакте"
    return Track(title=title, performer=performer, url=url, duration=seconds)


async def _search_with_retry(
    query: str, limit: int, token: str, db=None
) -> list[Track]:
    """Поиск с веб-токеном; если токен отозван — свежий из cookies и повтор."""

    async def attempt(web_token: str) -> list[Track]:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_web_search_sync, query, limit, web_token),
                timeout=SEARCH_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise MusicError(
                "Поиск занял слишком много времени, попробуй ещё раз."
            )
        except MusicError:
            raise
        except _VkWebTokenDead:
            # ВАЖНО: не превращаем в общий MusicError — наверху по этому
            # сигналу выпускается свежий токен из cookies (баг: раньше
            # общий except «съедал» его, и токен никогда не обновлялся).
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("VK поиск не удался: %s: %s", type(exc).__name__, exc)
            raise MusicError(
                "Не смог прочитать аудио ВКонтакте. Попробуй ещё раз через минуту."
            ) from exc

    try:
        items = await attempt(token)
    except _VkWebTokenDead:
        log.warning("VK: веб-токен не действует — создаю новый из cookies")
        try:
            fresh = await load_web_credentials(db, force_refresh=True)
        except _VkWebCookiesStale as exc:
            raise MusicError(
                "Сессия ВКонтакте в браузере устарела — обнови cookies: "
                "запусти scripts/vk_cookies.py и вставь новичок VK_WEB_COOKIES "
                "в Render."
            ) from exc
        items = await attempt(fresh or token)
    tracks = [track for track in (_track_from(item) for item in items) if track]
    # Никакой своей сортировки/дедупликации: берём первые 10 в том порядке,
    # в котором их отдал VK (просьба владельца 4 ноября 2026).
    unique = tracks[: max(1, limit)]
    if not unique:
        raise MusicError("Во ВКонтакте не нашёл подходящий трек 🤷 Уточни запрос.")
    return unique


async def find_tracks(
    query: str, *, limit: int = TRACKS_LIMIT, db=None
) -> list[Track]:
    """Поиск треков во ВКонтакте — ровно `limit` (10) в порядке выдачи VK."""
    query = " ".join(query.split())
    if not query:
        raise MusicError("Пустой запрос — напиши название или исполнителя 🙂")

    try:
        web_token = await load_web_credentials(db)
    except _VkWebCookiesStale as exc:
        web_token = None
        web_cookies_error = str(exc)
    except Exception as exc:  # noqa: BLE001
        web_token = None
        web_cookies_error = str(exc)
    else:
        web_cookies_error = ""

    if web_token:
        return await _search_with_retry(query, limit, web_token, db)

    # Веб-маршрут недоступен (нет cookies) — пробуем «мобильный» вход.
    try:
        credentials = await load_audio_credentials(db)
    except _VkNotConfigured as exc:
        raise MusicError(str(exc)) from exc
    except _VkAuthFailed as exc:
        raise MusicError(f"ВКонтакте не дал войти: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        log.warning("VK: вход не удался: %s", exc)
        raise MusicError(
            f"Не смог войти в ВКонтакте: {_human_auth_error(exc)}"
        ) from exc
    if not credentials:
        hint = (
            f"\nПричина по cookies: {web_cookies_error}."
            if web_cookies_error
            else ""
        )
        raise MusicError(
            "ВКонтакте не настроен. Варианты владельца:\n"
            "1) cookies из браузера: запусти scripts/vk_cookies.py и добавь "
            "VK_WEB_COOKIES в Render (пароль ВК не нужен);\n"
            "2) VK_AUDIO_TOKEN из scripts/vk_auth.py;\n"
            "3) VK_LOGIN + VK_PASSWORD."
            + hint
        )
    token, ua = credentials

    try:
        tracks = await asyncio.wait_for(
            asyncio.to_thread(_mobile_search_sync, query, limit, token, ua),
            timeout=SEARCH_TIMEOUT,
        )
    except _VkTokenDead:
        # Токен отозвали: сбрасываем кэш и вход и пробуем один раз заново.
        log.warning("VK: «мобильный» токен больше не действует — вхожу заново")
        reset_session()
        if db is not None:
            await db.kv_delete(VK_AUDIO_TOKEN_KV_KEY)
            await db.kv_delete(VK_AUDIO_UA_KV_KEY)
        fresh = await load_audio_credentials(db)
        if not fresh:
            raise MusicError(
                "ВКонтакте не настроен — добавь VK_LOGIN и VK_PASSWORD в Render."
            )
        token, ua = fresh
        try:
            tracks = await asyncio.wait_for(
                asyncio.to_thread(_mobile_search_sync, query, limit, token, ua),
                timeout=SEARCH_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise MusicError(
                "Поиск занял слишком много времени, попробуй ещё раз."
            )
        except MusicError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("VK повторный поиск не удался: %s", exc)
            raise MusicError(
                "Не смог прочитать аудио ВКонтакте. Попробуй ещё раз через минуту."
            ) from exc
    except asyncio.TimeoutError:
        raise MusicError("Поиск занял слишком много времени, попробуй ещё раз.")
    except MusicError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("VK поиск не удался: %s", exc)
        raise MusicError(
            "Не смог прочитать аудио ВКонтакте. Попробуй ещё раз через минуту."
        ) from exc

    if not tracks:
        raise MusicError(
            "Во ВКонтакте не нашёл подходящий трек 🤷 Уточни запрос."
        )
    return tracks


def _download_sync(url: str, out_dir: Path) -> tuple[Path, str]:
    """Скачиваем прямой mp3/аудио файл ВКонтакте. Возвращает (путь, расширение).

    Обычные requests/urllib vkuseraudio.net режет по TLS-отпечатку
    (403 без причины) — качаем через curl_cffi с имитацией Chrome,
    как это делает сам vkpymusic.
    """
    is_hls = ".m3u8" in url.lower()
    if is_hls:
        return _download_hls_sync(url, out_dir)

    from curl_cffi import requests as curl_requests  # локально: пак может не быть

    # ВАЖНО: у curl_cffi Response нет протокола контекстного менеджера
    # («with … as r» падает TypeError), и iter_content требует stream=True.
    response = curl_requests.get(
        url,
        impersonate="chrome110",
        headers={"Referer": "https://vk.ru/"},
        timeout=90,
        stream=True,
    )
    try:
        response.raise_for_status()
        extension = Path(urlparse(url).path).suffix.lower() or ".mp3"
        out_path = out_dir / f"audio{extension}"
        written = 0
        with open(out_path, "wb") as out_file:
            for chunk in response.iter_content(256 * 1024):
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_AUDIO_BYTES:
                    # Прерываем: файл больше лимита, не держим гигабиты.
                    raise RuntimeError("Трек ВКонтакте больше 30 МБ")
                out_file.write(chunk)
    finally:
        response.close()
    if written <= 0:
        raise RuntimeError("Файл трека ВК пришёл пустым")
    return out_path, extension


def _download_hls_sync(url: str, out_dir: Path) -> tuple[Path, str]:
    """HLS-плейлисты (m3u8) качает yt-dlp (с ffmpeg на борту)."""
    import yt_dlp

    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "outtmpl": str(out_dir / "audio.%(ext)s"),
        "max_filesize": MAX_AUDIO_BYTES,
        "retries": 2,
        "socket_timeout": 25,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.extract_info(url, download=True)
    files = [
        path for path in out_dir.iterdir()
        if path.is_file() and path.suffix.lower() != ".part"
    ]
    if not files:
        raise RuntimeError("Файл трека ВК (hls) не появился")
    return files[0], files[0].suffix


async def download_track(track: Track) -> tuple[bytes, str, int]:
    """Скачиваем аудио трека ВКонтакте, возвращаем (байты, имя файла, длина)."""
    last_error: Exception | None = None
    for attempt in range(2):
        with tempfile.TemporaryDirectory(prefix="bobby_vkmusic_") as tmp:
            out_dir = Path(tmp)
            try:
                path, extension = await asyncio.wait_for(
                    asyncio.to_thread(_download_sync, track.url, out_dir),
                    timeout=DOWNLOAD_TIMEOUT,
                )
                data = path.read_bytes()
                if not data:
                    raise MusicError("Файл трека ВКонтакте пришёл пустым.")
                if len(data) > MAX_AUDIO_BYTES:
                    raise MusicError("Трек больше 30 МБ — не качаю.")
                name = f"{_safe_name(track.title)}{extension}"
                return data, name, track.duration
            except asyncio.TimeoutError:
                last_error = RuntimeError("Скачивание не уложилось в таймаут")
            except MusicError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                log.info(
                    "VK скачивание не удалось (попытка %s): %s", attempt + 1, exc
                )
    log.warning(
        "Не удалось скачать трек ВК %s: %s(%s)",
        track.url[:80], type(last_error).__name__, last_error,
    )
    raise MusicError(
        "Не получилось скачать трек из ВКонтакте. Попробуй другой вариант "
        "или используй SoundCloud."
    ) from last_error


async def vk_configured(db=None) -> bool:
    """Настроен ли VK: cookies (веб), «мобильный» токен, логин/пароль или /vklogin."""
    if os.environ.get(VK_WEB_COOKIES_ENV, "").strip():
        return True
    if os.environ.get("VK_AUDIO_TOKEN", "").strip():
        return True
    if db is not None and (await db.kv_get(VK_WEB_COOKIES_KV_KEY)):
        return True
    if db is not None and (await db.kv_get(VK_AUDIO_TOKEN_KV_KEY)):
        return True
    login, password, _ = _credentials()
    if login and password:
        return True
    return bool(await load_token(db))
