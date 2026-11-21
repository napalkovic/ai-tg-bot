"""Аккуратный вывод: markdown -> HTML для Telegram и разбиение длинных сообщений."""

from __future__ import annotations

import html
import re

TG_LIMIT = 4096
SAFE_LIMIT = 3900  # оставляем запас под HTML-теги

_FENCE = re.compile(r"```([^\n`]*)\n?(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.DOTALL)
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])|(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])")
_STRIKE = re.compile(r"~~(.+?)~~", re.DOTALL)
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_HEADER = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_HR = re.compile(r"^\s*(?:---+|\*\*\*+|___+)\s*$", re.MULTILINE)
_BULLET = re.compile(r"^(\s*)[-*+]\s+", re.MULTILINE)

_PLACEHOLDER = "\x00{}\x00"


def md_to_html(text: str) -> str:
    """Мини-конвертер markdown в HTML, понятный Telegram.

    Не претендует на полноту: цель — чтобы код, жирный текст и ссылки
    выглядели нормально. При любой ошибке вызывающий код откатывается на
    обычный текст.
    """
    stash: list[str] = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return _PLACEHOLDER.format(len(stash) - 1)

    # 1. Код вырезаем до экранирования, чтобы не ломать его разметкой.
    def take_fence(match: re.Match) -> str:
        lang = match.group(1).strip()
        body = html.escape(match.group(2).rstrip("\n"), quote=False)
        if lang:
            return keep(f'<pre><code class="language-{html.escape(lang, quote=True)}">{body}</code></pre>')
        return keep(f"<pre>{body}</pre>")

    text = _FENCE.sub(take_fence, text)

    def take_code(match: re.Match) -> str:
        return keep(f"<code>{html.escape(match.group(1), quote=False)}</code>")

    text = _INLINE_CODE.sub(take_code, text)

    # 2. Всё остальное экранируем и размечаем.
    text = html.escape(text, quote=False)

    text = _LINK.sub(lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>', text)
    text = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    text = _STRIKE.sub(r"<s>\1</s>", text)
    text = _ITALIC.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", text)
    text = _HEADER.sub(lambda m: f"<b>{m.group(2).strip()}</b>", text)
    text = _HR.sub("──────────", text)
    text = _BULLET.sub(lambda m: f"{m.group(1)}• ", text)

    # 3. Возвращаем код на место.
    for index, fragment in enumerate(stash):
        text = text.replace(_PLACEHOLDER.format(index), fragment)
    return text.strip()


def split_text(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Режем длинный текст по границе строки/слова."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    if rest:
        parts.append(rest)
    return parts


def preview(text: str, limit: int = 60) -> str:
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"
