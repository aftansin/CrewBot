"""Конфигурация приложения. Единственное место, где читается окружение."""

from __future__ import annotations

import re
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Telegram ---
    bot_token: str
    # Читается строкой, а не list[int]: для составных типов pydantic-settings
    # сначала пробует разобрать значение как JSON, и "123,456" на этом падает.
    admin_ids_raw: str = Field(default="", validation_alias="ADMIN_IDS")

    # --- База данных ---
    database_url: str = "postgresql+asyncpg://crewbot:crewbot@db:5432/crewbot"

    # --- Календарь ---
    # Префикс, который обязан иметь URL подписки. Защищает от того,
    # что пользователь вставит в бота произвольную ссылку.
    ics_url_prefix: str = "https://crew.aeroflot.ru/api/calendar/ics/"
    http_timeout_seconds: int = 30
    http_retries: int = 3

    # --- Опрос ---
    # Интервал по умолчанию для новых пилотов, в минутах.
    poll_interval_minutes: int = 180
    poll_interval_min_minutes: int = 15
    poll_interval_max_minutes: int = 1440
    # Случайный разброс старта задач, чтобы все пилоты не ломились
    # к провайдеру в одну и ту же секунду.
    poll_jitter_seconds: int = 300

    # --- Предохранитель ---
    # Если из ленты за один проход пропала доля будущих событий больше этой,
    # синхронизация отменяется целиком и админу уходит предупреждение.
    # Защита от неполного/битого ответа провайдера.
    cancel_guard_ratio: float = 0.5
    cancel_guard_min_events: int = 4

    # --- Прочее ---
    default_timezone: str = "Europe/Moscow"
    log_level: str = "INFO"

    @field_validator("default_timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:  # pragma: no cover - конфиг-ошибка
            raise ValueError(f"Неизвестная таймзона: {value}") from exc
        return value

    @property
    def default_tz(self) -> ZoneInfo:
        return ZoneInfo(self.default_timezone)

    @property
    def admin_ids(self) -> list[int]:
        """Разбирает "123, 456" в список id. Мусор молча отбрасывается."""
        result: list[int] = []
        for part in re.split(r"[,;\s]+", self.admin_ids_raw):
            part = part.strip()
            if not part:
                continue
            try:
                result.append(int(part))
            except ValueError:
                continue
        return result

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
