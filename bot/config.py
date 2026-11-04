"""Конфигурация бота: читается из .env и переменных окружения."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


class ConfigError(RuntimeError):
    """Понятная человеку ошибка конфигурации (бот не стартует)."""


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _env_bool(name: str, default: bool = False) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on", "y", "да"}


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    value = _env(name)
    try:
        return float(value) if value is not None else default
    except ValueError:
        return default


def _clean_secret(value: str) -> str:
    """Убираем кавычки и префикс Bearer, если их вставили вместе с ключом."""
    value = value.strip().strip('"').strip("'").strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    return value


def _clean_account_id(value: str) -> str:
    """Принимаем и голый Account ID, и ссылку из адресной строки Cloudflare."""
    value = _clean_secret(value)
    match = re.search(r"\b[0-9a-f]{32}\b", value, re.IGNORECASE)
    return match.group(0) if match else value


# --- провайдеры, совместимые с OpenAI API --------------------------------

# base_url, модель по умолчанию, подпись для меню
PRESETS: dict[str, tuple[str, str, str]] = {
    "openrouter": (
        "https://openrouter.ai/api/v1",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "OpenRouter",
    ),
    "groq": (
        "https://api.groq.com/openai/v1",
        "llama-3.3-70b-versatile",
        "Groq",
    ),
    "deepseek": (
        "https://api.deepseek.com",
        "deepseek-chat",
        "DeepSeek API (офиц.)",
    ),
}

DEEPSEEK_WEB = "deepseek_web"

# Модели, которые умеют смотреть картинки (используются, когда приходит фото).
VISION_DEFAULTS: dict[str, str] = {
    "openrouter": "nvidia/nemotron-3.5-super-vl:free",
    "groq": "meta-llama/llama-4-scout-17b-16e-instruct",
}

# Генерация картинок: бесплатная модель Cloudflare Workers AI (FLUX.1 schnell).
# Бесплатный тариф — 10 000 «нейронов» в день, это сотни картинок, без карты.
DEFAULT_IMAGE_MODEL = "@cf/black-forest-labs/flux-1-schnell"


@dataclass(frozen=True)
class ProviderConfig:
    """Один источник ответов."""

    key: str  # "deepseek_web" либо ключ пресета
    label: str
    kind: str  # "deepseek_web" | "openai"
    base_url: str = ""
    model: str = ""
    api_key: str = ""


@dataclass(frozen=True)
class Settings:
    bot_token: str
    owner_ids: frozenset[int]
    db_path: Path
    db_url: str | None
    port: int | None
    session_dir: Path

    system_prompt: str
    memory_max_messages: int
    memory_max_chars: int
    history_on_resume: bool

    default_provider: str
    deepseek_web: ProviderConfig | None
    fallback: ProviderConfig | None

    ds_app_version: str
    ds_min_interval: float
    ds_cooldown: float
    ds_refresh_minutes: int
    ds_session_max_hours: int

    vision_provider: str | None
    vision_model: str

    # Генерация картинок
    image_model: str
    image_free_fallback: bool
    cloudflare_account_id: str
    cloudflare_api_token: str
    pollinations_token: str

    # Бесплатные HF Space для картинок (FLUX.1-dev и запасной SD 3.5).
    image_hf_space: str
    image_hf_space_fallback: str
    image_hf_steps: int

    # Генерация видео (бесплатные HF Space) и общий HF-токен (необязательно).
    video_seconds: float
    hf_token: str

    request_timeout: float

    def provider(self, key: str) -> ProviderConfig | None:
        if key == DEEPSEEK_WEB:
            return self.deepseek_web
        if self.fallback and key == self.fallback.key:
            return self.fallback
        return None

    def vision_config(self) -> ProviderConfig | None:
        """Провайдер для картинок. DeepSeek-веб фото не принимает."""
        if not self.vision_provider or not self.vision_model:
            return None
        base = self.provider(self.vision_provider)
        if base is None or base.kind != "openai":
            return None
        return ProviderConfig(
            key=base.key,
            label=base.label,
            kind=base.kind,
            base_url=base.base_url,
            model=self.vision_model,
            api_key=base.api_key,
        )

    def available_providers(self) -> list[ProviderConfig]:
        items: list[ProviderConfig] = []
        if self.deepseek_web is not None:
            items.append(self.deepseek_web)
        if self.fallback is not None:
            items.append(self.fallback)
        return items

    def cloudflare_ready(self) -> bool:
        return bool(self.cloudflare_account_id and self.cloudflare_api_token)


def _load_deepseek_web() -> ProviderConfig | None:
    # Токен можно положить в .env (DEEPSEEK_TOKEN) или получить скриптом
    # scripts/ds_login.sh — тогда он лежит в session/session.json.
    explicit_token = _env("DEEPSEEK_TOKEN")
    session_dir = Path(_env("DEEPSEEK_SESSION_DIR", str(ROOT / "session"))).expanduser()
    has_session = (session_dir / "session.json").exists()

    if not _env_bool("DEEPSEEK_WEB_ENABLED", True):
        return None
    if not explicit_token and not has_session:
        return None
    return ProviderConfig(key=DEEPSEEK_WEB, label="DeepSeek (веб, бесплатно)", kind="deepseek_web")


def _load_fallback() -> ProviderConfig | None:
    key = _env("FALLBACK_PROVIDER", "openrouter")
    if key is None or key.lower() in {"none", "off", "no", "нет"}:
        return None

    api_key = (
        _env(f"{key.upper()}_API_KEY")
        or _env("FALLBACK_API_KEY")
        or _env("OPENAI_API_KEY")
        or ""
    )

    if key == "custom":
        base_url = _env("FALLBACK_BASE_URL")
        model = _env("FALLBACK_MODEL")
        if not base_url or not model:
            return None
        label = "Своя модель"
    elif key in PRESETS:
        base_url, preset_model, label = PRESETS[key]
        model = _env("FALLBACK_MODEL", preset_model)
    else:
        raise ConfigError(
            f"FALLBACK_PROVIDER={key!r} неизвестен. "
            f"Доступные: {', '.join(sorted(PRESETS))}, custom, none."
        )

    if not api_key:
        return None
    return ProviderConfig(
        key=key, label=label, kind="openai",
        base_url=base_url, model=model or "", api_key=api_key,
    )


def load_settings() -> Settings:
    token = _env("BOT_TOKEN")
    if not token:
        raise ConfigError(
            "BOT_TOKEN не задан. Создай бота у @BotFather и впиши токен в .env "
            "(см. .env.example)."
        )
    if ":" not in token:
        raise ConfigError("BOT_TOKEN выглядит неверно — ожидается формат 123456789:ABC...")
    if token.startswith("123456789:"):
        raise ConfigError("BOT_TOKEN всё ещё из примера. Впиши настоящий токен от @BotFather.")

    raw_owners = _env("OWNER_IDS", "")
    owner_ids: set[int] = set()
    for chunk in raw_owners.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            owner_ids.add(int(chunk))
        except ValueError:
            raise ConfigError(f"OWNER_IDS: {chunk!r} не похоже на Telegram ID (число).")
    if not owner_ids:
        raise ConfigError(
            "OWNER_IDS пуст. Бот приватный: укажи свой Telegram ID (узнать: @userinfobot)."
        )

    deepseek_web = _load_deepseek_web()
    fallback = _load_fallback()

    if deepseek_web is None and fallback is None:
        raise ConfigError(
            "Нет ни одного источника ответов.\n"
            "  • DeepSeek веб: запусти `./scripts/ds_login.sh` (сохранит session/session.json).\n"
            "  • Бесплатная модель: впиши FALLBACK_PROVIDER + API-ключ в .env."
        )

    requested = _env("DEFAULT_PROVIDER", DEEPSEEK_WEB if deepseek_web else fallback.key)
    available = {p.key: p for p in (deepseek_web, fallback) if p}
    if requested in available:
        default_provider = requested
    elif requested == DEEPSEEK_WEB and deepseek_web is None:
        # DeepSeek ещё не подключён — работаем на резервной модели.
        default_provider = fallback.key
    else:
        raise ConfigError(
            f"DEFAULT_PROVIDER={requested!r} недоступен. "
            f"Доступные: {', '.join(available)}"
        )

    db_path = Path(_env("DB_PATH", str(ROOT / "data" / "bot.sqlite3"))).expanduser()
    db_url = _env("DATABASE_URL")
    port = _env_int("PORT", 0) or None
    session_dir = Path(
        _env("DEEPSEEK_SESSION_DIR", str(ROOT / "session"))
    ).expanduser()

    # Кто смотрит картинки: по умолчанию резервная модель (у DeepSeek-веба
    # зрения нет). DeepSeek-веб фото не принимает.
    vision_provider = _env("VISION_PROVIDER")
    if vision_provider is None and fallback is not None and fallback.kind == "openai":
        vision_provider = fallback.key
    if vision_provider and not available.get(vision_provider):
        vision_provider = fallback.key if fallback is not None else None
    vision_model = _env("VISION_MODEL", VISION_DEFAULTS.get(vision_provider or "", "")) or ""
    if vision_provider and not vision_model:
        vision_provider = None

    image_model = _env("IMAGE_MODEL", DEFAULT_IMAGE_MODEL) or DEFAULT_IMAGE_MODEL

    return Settings(
        bot_token=token,
        owner_ids=frozenset(owner_ids),
        db_path=db_path,
        db_url=db_url,
        port=port,
        session_dir=session_dir,
        system_prompt=_env(
            "SYSTEM_PROMPT",
            "Тебя зовут Бобби (Bobby). Ты — личный ИИ-ассистент. Отвечай на языке "
            "пользователя, по делу и без лишней воды. Оформляй ответы аккуратно. "
            "Ты никогда не называешь себя названием модели или компании — ты Бобби. "
            "Ты умеешь рисовать картинки по описанию, поэтому никогда не говори, "
            "что ты «только текстовый». На вопрос «кто ты» отвечай: «Я Бобби, твой "
            "личный ассистент».",
        ),
        memory_max_messages=_env_int("MEMORY_MAX_MESSAGES", 40),
        memory_max_chars=_env_int("MEMORY_MAX_CHARS", 24000),
        history_on_resume=_env_bool("HISTORY_ON_RESUME", True),
        default_provider=default_provider,
        deepseek_web=deepseek_web,
        fallback=fallback,
        ds_app_version=_env("DEEPSEEK_APP_VERSION", "2.0.0"),
        ds_min_interval=float(_env("DEEPSEEK_MIN_INTERVAL", "3")),
        ds_cooldown=float(_env("DEEPSEEK_COOLDOWN", "60")),
        ds_refresh_minutes=_env_int("DEEPSEEK_REFRESH_MINUTES", 300),
        ds_session_max_hours=_env_int("DEEPSEEK_SESSION_MAX_HOURS", 6),
        vision_provider=vision_provider,
        vision_model=vision_model,
        image_model=image_model,
        image_free_fallback=_env_bool("IMAGE_FREE_FALLBACK", True),
        cloudflare_account_id=_clean_account_id(_env("CLOUDFLARE_ACCOUNT_ID", "") or ""),
        cloudflare_api_token=_clean_secret(_env("CLOUDFLARE_API_TOKEN", "") or ""),
        pollinations_token=_clean_secret(_env("POLLINATIONS_TOKEN", "") or ""),
        image_hf_space=_env("IMAGE_HF_SPACE", "black-forest-labs/FLUX.1-dev")
        or "black-forest-labs/FLUX.1-dev",
        image_hf_space_fallback=_env(
            "IMAGE_HF_SPACE_FALLBACK", "stabilityai/stable-diffusion-3.5-large"
        )
        or "stabilityai/stable-diffusion-3.5-large",
        image_hf_steps=max(4, min(_env_int("IMAGE_HF_STEPS", 24), 40)),
        video_seconds=max(2.0, min(_env_float("VIDEO_SECONDS", 2.0), 5.0)),
        hf_token=_clean_secret(_env("HF_TOKEN", "") or ""),
        request_timeout=float(_env("REQUEST_TIMEOUT", "180")),
    )
