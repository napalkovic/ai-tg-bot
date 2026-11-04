"""Telegram-бот с ИИ: пакет приложения."""

from __future__ import annotations

import sys
from pathlib import Path

# Вендорная библиотека DeepSeek живёт в vendor/ и импортируется как `deepseek`.
_VENDOR = Path(__file__).resolve().parent.parent / "vendor"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))
