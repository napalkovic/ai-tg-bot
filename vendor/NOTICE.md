# Вендорный код

`vendor/deepseek/` — копия пакета `deepseek` из проекта
[Tsuev/opencode-deepseek](https://github.com/Tsuev/opencode-deepseek),
лицензия MIT (см. `LICENSE-deepseek-bridge.txt`).

Пакет реализует неофициальный доступ к веб-чату chat.deepseek.com:
вход через Playwright, затем чистый HTTP + решение proof-of-work через
родной WebAssembly-модуль DeepSeek.

## Локальные изменения

1. `auth.py` — импорт Playwright сделан ленивым (`_sync_playwright()`),
   чтобы клиент работал без установленного Playwright, когда сессия уже есть.
2. `__init__.py` — экспорты через `__getattr__` по той же причине.

Больше ничего не менялось, чтобы проще было обновлять код из апстрима.
