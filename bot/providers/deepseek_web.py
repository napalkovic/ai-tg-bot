"""DeepSeek через веб-чат (неофициально, по токену аккаунта).

Использует вендорную библиотеку `vendor/deepseek` (MIT): она ходит в
chat.deepseek.com напрямую, решает proof-of-work через родной WASM DeepSeek.

Библиотека синхронная, поэтому все запросы выполняются в одном отдельном
потоке (Store у wasmtime не потокобезопасен), а результат отдаётся в asyncio
через очередь.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import AsyncIterator

import httpx

from ..config import ProviderConfig, Settings
from .base import AuthRequired, Delta, Done, Event, ProviderError, RateLimited

log = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def load_session(settings: Settings):
    """Достаём сессию DeepSeek: из .env (токен) или из файла, оставленного входом.

    На сервере без браузера работает только первый вариант — тогда сессия не
    зависит от наличия профиля Chrome.
    """
    from deepseek.auth import LoginRequired, Session, get_session

    token = os.getenv("DEEPSEEK_TOKEN", "").strip()
    if token:
        cookies: dict[str, str] = {}
        raw = os.getenv("DEEPSEEK_COOKIES", "").strip()
        if raw:
            try:
                cookies = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise AuthRequired(
                    "DEEPSEEK_COOKIES должен быть JSON-объектом вида "
                    '{"cookie_name": "value"}.'
                ) from exc
        offset = os.getenv("DEEPSEEK_TIMEZONE_OFFSET")
        return Session(
            token=token,
            cookies=cookies,
            user_agent=os.getenv("DEEPSEEK_USER_AGENT", DEFAULT_UA),
            captured_at=time.time(),
            locale=os.getenv("DEEPSEEK_LOCALE") or "ru_RU",
            timezone_offset=int(offset) if offset and offset.lstrip("-").isdigit() else None,
        )

    try:
        return get_session(
            profile_dir=settings.session_dir / "profile",
            session_file=settings.session_dir / "session.json",
            max_age=max(1, settings.ds_session_max_hours) * 3600,
            allow_interactive=False,
        )
    except LoginRequired as exc:
        raise AuthRequired(str(exc)) from exc


class _RetryAuth(Exception):
    """Внутренний сигнал: токен отклонён, надо пересоздать клиент."""


class DeepSeekWebProvider:
    key = "deepseek_web"

    def __init__(self, cfg: ProviderConfig, settings: Settings):
        self.cfg = cfg
        self.settings = settings
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="deepseek-web")
        self._client = None
        self._client_lock = threading.Lock()
        self._pace_lock = threading.Lock()
        self._last_started = 0.0
        self._cooldown_until = 0.0

    # --- синхронная часть (в отдельном потоке) ---------------------------

    def _build_client(self):
        os.environ.setdefault("DEEPSEEK_APP_VERSION", self.settings.ds_app_version)
        try:
            from deepseek.client import DeepSeekClient
        except Exception as exc:  # нет wasmtime / нет вендорной папки
            raise AuthRequired(
                "Не удалось загрузить клиент DeepSeek: "
                f"{exc}. Проверь, что установлены зависимости из requirements.txt."
            ) from exc
        return DeepSeekClient(load_session(self.settings), allow_interactive=False)

    def _client_or_refresh(self, force: bool = False):
        with self._client_lock:
            if self._client is None or force:
                old, self._client = self._client, None
                if old is not None:
                    try:
                        old.close()
                    except Exception:
                        pass
                self._client = self._build_client()
            return self._client

    def _pace(self) -> None:
        """Небольшая пауза между запросами: бережём аккаунт от блокировок."""
        with self._pace_lock:
            now = time.monotonic()
            wait = self._last_started + self.settings.ds_min_interval - now
            if wait > 0:
                time.sleep(min(wait, 30))
            self._last_started = time.monotonic()

    def _stream_once(
        self,
        client,
        prompt: str,
        cid: str | None,
        model: str,
        thinking: bool,
        search: bool,
        emit,
        cancel: threading.Event,
    ) -> tuple[int, str | None]:
        stream = client.stream(
            prompt,
            conversation_id=cid,
            model=None if cid else (model or "default"),
            thinking=thinking,
            search=search,
        )
        chars = 0
        for chunk in stream:
            if cancel.is_set():
                return chars, None
            if chunk:
                chars += len(chunk)
                emit(("delta", chunk))
        return chars, stream.conversation_id

    def _run(self, prompt: str, cid: str | None, model: str,
             thinking: bool, search: bool, emit, cancel: threading.Event) -> None:
        client = self._client_or_refresh()
        self._pace()
        try:
            chars, new_cid = self._stream_once(
                client, prompt, cid, model, thinking, search, emit, cancel
            )
            if cancel.is_set():
                return

            # Веб-поиск у DeepSeek иногда включён на аккаунте, но ответ приходит
            # пустым. Тогда повторяем тот же вопрос без поиска — лучше ответ без
            # источников, чем ничего.
            if chars == 0 and search:
                log.warning(
                    "DeepSeek: пустой ответ при включённом веб-поиске — "
                    "повторяю без поиска"
                )
                self._pace()
                chars, new_cid = self._stream_once(
                    client, prompt, cid, model, thinking, False, emit, cancel
                )
                if cancel.is_set():
                    return

            emit(("done", new_cid, chars))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                raise _RetryAuth() from exc
            raise

    def _worker(self, prompt: str, cid: str | None, model: str,
                thinking: bool, search: bool, emit, cancel: threading.Event) -> None:
        try:
            try:
                self._run(prompt, cid, model, thinking, search, emit, cancel)
            except _RetryAuth:
                log.warning("DeepSeek отклонил сессию, пересоздаю клиент")
                self._client_or_refresh(force=True)
                self._run(prompt, cid, model, thinking, search, emit, cancel)
        except BaseException as exc:  # noqa: BLE001 - уходит в asyncio
            emit(("error", exc))

    # --- промпт ----------------------------------------------------------

    @staticmethod
    def _flatten(content) -> str:
        """Простая реплика может быть строкой или списком parts (фото-повороты)."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text") or "")
            return " ".join(parts)
        return ""

    # Напоминание о недавней истории, чтобы основная модель была в курсе
    # реплик, которые шли мимо неё (например, фото смотрела зрячая модель,
    # а отвечал DeepSeek о нём уже в следующем сообщении).
    RECAP_MAX_MESSAGES = 20
    RECAP_MAX_CHARS = 6000
    RECAP_ITEM_MAX = 400

    def _recap(self, history: list[dict]) -> str:
        if not self.settings.history_on_resume:
            return ""
        entries: list[str] = []
        total = 0
        for message in reversed(history):
            text = self._flatten(message.get("content")).strip()
            if not text:
                continue
            if len(text) > self.RECAP_ITEM_MAX:
                text = text[: self.RECAP_ITEM_MAX - 1].rstrip() + "…"
            who = "Пользователь" if message.get("role") == "user" else "Бобби"
            entry = f"• {who}: {text}"
            if entries and (
                len(entries) >= self.RECAP_MAX_MESSAGES
                or total + len(entry) > self.RECAP_MAX_CHARS
            ):
                break
            entries.append(entry)
            total += len(entry)
        if not entries:
            return ""
        entries.reverse()
        return (
            "Напоминание — что происходило в этом чате раньше "
            "(это тоже наша общая тема):"
            + "\n" + "\n".join(entries)
        )

    def _build_prompt(self, messages: list[dict], resume: bool) -> str:
        last = self._flatten(messages[-1]["content"]) if messages else ""
        # Личность напоминаем в каждом запросе: иначе старая ветка DeepSeek
        # помнит только прежнюю инструкцию и продолжает звать себя DeepSeek.
        persona = f"[Инструкция: {self.settings.system_prompt}]"
        if resume:
            # Ветка на стороне DeepSeek помнит свою часть истории, но реплики,
            # проходившие через другие модели (зрение и т.п.), до неё не дошли —
            # подмешиваем компактное напоминание о недавнем.
            recap = self._recap(messages[:-1])
            if recap:
                return f"{persona}\n\n{recap}\n\n{last}"
            return f"{persona}\n\n{last}"

        history = messages[:-1]
        if not history:
            return f"{persona}\n\n{last}"

        lines = [
            persona,
            "",
            "Ниже история нашего диалога. Продолжи его.",
            "",
        ]
        for message in history:
            who = "Пользователь" if message["role"] == "user" else "Ассистент"
            lines.append(f"{who}: {message['content']}")
        lines += ["", f"Пользователь: {last}"]
        return "\n".join(lines)

    # --- публичный async-интерфейс --------------------------------------

    async def stream(
        self,
        messages: list[dict],
        *,
        cid: str | None = None,
        model: str = "",
        thinking: bool = False,
        search: bool = False,
    ) -> AsyncIterator[Event]:
        now = time.monotonic()
        if now < self._cooldown_until:
            left = int(self._cooldown_until - now) + 1
            raise RateLimited(
                f"DeepSeek на паузе ещё {left} с — сработала защита от частых запросов.",
                retry_after=float(left),
            )

        prompt = self._build_prompt(messages, resume=bool(cid))
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        cancel = threading.Event()

        def emit(item) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:
                pass  # цикл уже закрыт

        future = loop.run_in_executor(
            self._executor,
            self._worker,
            prompt, cid, model, thinking, search, emit, cancel,
        )

        def _swallow(fut) -> None:
            try:
                fut.exception()
            except Exception:
                pass

        future.add_done_callback(_swallow)
        try:
            while True:
                item = await queue.get()
                kind = item[0]
                if kind == "delta":
                    yield Delta(item[1])
                elif kind == "done":
                    if not item[2]:
                        raise ProviderError(
                            "DeepSeek вернул пустой ответ — возможно, сработала "
                            "защита или закончился лимит на аккаунте."
                        )
                    yield Done(cid=item[1])
                    return
                else:
                    raise self._user_error(item[1])
        finally:
            cancel.set()

    def _user_error(self, exc: BaseException) -> ProviderError:
        if isinstance(exc, ProviderError):
            return exc
        if isinstance(exc, httpx.HTTPStatusError):
            code = exc.response.status_code
            if code in (401, 403):
                return AuthRequired(
                    "DeepSeek не принял сессию (401/403). Запусти "
                    "`./scripts/ds_login.sh`, чтобы войти заново."
                )
            if code == 429:
                self._enter_cooldown()
                return RateLimited(
                    "DeepSeek ограничил частоту запросов. Подожди немного.",
                    retry_after=self.settings.ds_cooldown,
                )
            return ProviderError(f"DeepSeek ответил ошибкой HTTP {code}.")
        text = str(exc)
        low = text.lower()
        if any(k in low for k in ("login", "unauthorized", "token", "auth")):
            return AuthRequired(
                "Сессия DeepSeek недействительна. Запусти `./scripts/ds_login.sh`, "
                "чтобы войти заново."
            )
        if any(k in low for k in ("rate limit", "too many", "risk", "429", "频繁")):
            self._enter_cooldown()
            return RateLimited(
                "DeepSeek ограничил частоту запросов. Подожди немного.",
                retry_after=self.settings.ds_cooldown,
            )
        return ProviderError(f"Ошибка DeepSeek: {text[:300]}")

    def _enter_cooldown(self) -> None:
        self._cooldown_until = time.monotonic() + self.settings.ds_cooldown
        log.warning("DeepSeek: пауза %.0f с", self.settings.ds_cooldown)

    async def aclose(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
