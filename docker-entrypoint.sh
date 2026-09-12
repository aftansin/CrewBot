#!/bin/sh
set -e

echo "Применяю миграции..."
alembic upgrade head

echo "Запускаю бота..."
exec "$@"
