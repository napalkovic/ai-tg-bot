FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=10000

WORKDIR /app

# ffmpeg нужен для конвертации музыки в mp3 (раздел «Сервисы»).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot
COPY vendor ./vendor

EXPOSE 10000

# На хостингах (Render и т.п.) порт приходит в переменной PORT,
# база — в DATABASE_URL; локально можно монтировать data/ и session/.
CMD ["python", "-m", "bot"]
