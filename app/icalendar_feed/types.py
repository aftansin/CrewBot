"""Промежуточное представление события между парсером и остальным кодом."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime

from app.db.models import EventKind


@dataclass(slots=True)
class ParsedEvent:
    uid: str
    kind: EventKind
    summary: str
    description: str
    dtstart: datetime  # всегда timezone-aware
    dtend: datetime    # всегда timezone-aware
    location: str | None = None
    flight_no: str | None = None
    dep_code: str | None = None
    dep_city: str | None = None
    arr_code: str | None = None
    arr_city: str | None = None
    aircraft: str | None = None
    crew: list[str] = field(default_factory=list)

    def content_hash(self) -> str:
        """Отпечаток значимых полей.

        Описание нормализуется: провайдер тасует пробелы и порядок строк
        между выгрузками, и без нормализации бот слал бы "изменения"
        на пустом месте.
        """
        # Порядок важен: сначала обрезаем пробелы, потом сортируем.
        # Если сортировать сырые строки, отступы влияют на порядок и хэш
        # расходится там, где содержимое не менялось.
        normalized_description = "\n".join(
            sorted(
                line.strip() for line in self.description.splitlines() if line.strip()
            )
        )
        payload = "\x1f".join(
            [
                self.uid,
                self.kind.value,
                " ".join(self.summary.split()),
                normalized_description,
                self.dtstart.astimezone(tz=self.dtstart.tzinfo).isoformat(),
                self.dtend.astimezone(tz=self.dtend.tzinfo).isoformat(),
                self.location or "",
            ]
        )
        return hashlib.md5(payload.encode("utf-8")).hexdigest()
