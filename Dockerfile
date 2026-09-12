FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC

WORKDIR /app

# Отдельным слоем: пересборка зависимостей только при их изменении.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Бот не имеет причин работать от root.
RUN useradd --create-home --uid 10001 crewbot && chown -R crewbot:crewbot /app
USER crewbot

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "app.main"]
