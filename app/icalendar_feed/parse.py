"""Разбор ленты iCalendar.

ЭТО ЕДИНСТВЕННЫЙ МОДУЛЬ, ЗАВИСЯЩИЙ ОТ ФОРМАТА ПРОВАЙДЕРА.
Остальное приложение работает с ``ParsedEvent`` и про эмодзи со скобками
ничего не знает.

Что выяснилось на реальной ленте:

* ``X-WR-CALNAME`` содержит не ФИО, а название расписания. Владельца
  приходится определять по составу экипажей;
* ФИО в описаниях записаны латиницей ("Ivanov Ivan Ivanovich"),
  а должности кириллицей: (КВС), (2П), (СБ);
* эмодзи стоит в начале SUMMARY и является главным признаком типа события,
  поэтому очистка строки выполняется ПОСЛЕ классификации;
* SUMMARY бывает многострочным: первая строка — заголовок, дальше детали;
* в описании рейса есть тип борта: ``[SU1562, B-737-800H]``.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from icalendar import Calendar

from app.db.models import EventKind
from app.icalendar_feed.types import ParsedEvent

logger = logging.getLogger(__name__)


class ParseError(Exception):
    pass


# --------------------------------------------------------------------------
# Классификация типа события
# --------------------------------------------------------------------------

# Эмодзи-маркер в начале SUMMARY — самый надёжный признак. Проверяется первым.
EMOJI_RULES: tuple[tuple[EventKind, tuple[str, ...]], ...] = (
    # Перелёт пассажиром: чемодан. Проверяется ПЕРВЫМ, до самолёта —
    # в таком событии есть оба значка, но налётом это не является.
    (EventKind.DEADHEAD, ("\U0001f9f3",)),                # чемодан
    (EventKind.FLIGHT, ("\u2708",)),                      # самолёт
    (EventKind.MEDICAL, ("\U0001f489", "\U0001fa7a")),    # шприц, стетоскоп
    (EventKind.REST, ("\U0001f3dd", "\U0001f334", "\U0001f3e0")),  # остров, пальма, дом
    (EventKind.TRAINING, ("\U0001f468\u200d\U0001f393",   # студент
                          "\U0001f469\u200d\U0001f393",
                          "\U0001f393",
                          "\U0001f4da")),
    # Планшет — reporting, НО тренажёр под тем же значком ловится
    # ключевым словом ниже раньше, чем сюда дойдёт.
    (EventKind.REPORTING, ("\U0001f4cb",)),               # планшет
)

# Запасной проход по ключевым словам, если эмодзи ничего не дал.
KEYWORD_RULES: tuple[tuple[EventKind, tuple[str, ...]], ...] = (
    (EventKind.MEDICAL, ("влэк", "медкомисс", "медосмотр", "медицинск")),
    (EventKind.SIMULATOR, ("тренаж", "симулятор", "fftd", " ftd", " ffs", "simulator")),
    (EventKind.REST, ("отпуск", "выходн", "отдых", "больничн")),
    (EventKind.REPORTING, ("reporting", "явка")),
    (EventKind.TRAINING, ("обучен", "учеб", "учёб", "занят", "подготовк", "курс",
                          "зачет", "зачёт", "экзамен", "брифинг", "теори", "training")),
    (EventKind.STANDBY, ("резерв", "дежурств", "готовност", "standby")),
    (EventKind.DUTY, ("наземн", "совещан", "собран", "офис")),
)


# Слова, которые должны переопределить эмодзи-планшет: под 📋 приходит
# и явка, и тренажёр, а это разные вещи.
OVERRIDE_KEYWORDS: tuple[tuple[EventKind, tuple[str, ...]], ...] = (
    (EventKind.SIMULATOR, ("simulator", "тренаж", "симулятор", "fftd", " ftd", " ffs")),
)


def classify(summary: str) -> EventKind:
    """Определяет тип события по СЫРОМУ summary, до очистки от эмодзи."""
    haystack = " " + summary.lower()

    # Переопределения идут первыми: "Flight Simulator" под значком планшета
    # иначе разберётся как явка.
    for kind, keywords in OVERRIDE_KEYWORDS:
        if any(word in haystack for word in keywords):
            return kind

    for kind, markers in EMOJI_RULES:
        if any(marker in summary for marker in markers):
            return kind

    for kind, keywords in KEYWORD_RULES:
        if any(word in haystack for word in keywords):
            return kind

    return EventKind.OTHER


# --------------------------------------------------------------------------
# Разбор строки рейса
# --------------------------------------------------------------------------

# "SU1562 [самолёт] Moscow (SVO | Sheremetyevo (B)) -> Irkutsk (IKT | Irkutsk)"
FLIGHT_NO_RE = re.compile(r"\b([A-Z]{2}\s?\d{1,4})\b")
# Название города перед скобкой и IATA-код в скобках до вертикальной черты.
AIRPORT_RE = re.compile(r"([^(\u2192]+?)\s*\(\s*([A-Z]{3})\s*\|")
ARROW_RE = re.compile(r"[\u2192\u2794\u279c>]+")
# "[SU1562, B-737-800H]" в начале описания рейса.
AIRCRAFT_RE = re.compile(r"\[\s*[A-Z]{2}\s?\d{1,4}\s*,\s*([^\]]+?)\s*\]")


def parse_flight_route(summary: str) -> dict[str, str | None]:
    """Достаёт номер рейса и пару аэропортов. Все поля опциональны."""
    result: dict[str, str | None] = {
        "flight_no": None,
        "dep_code": None,
        "dep_city": None,
        "arr_code": None,
        "arr_city": None,
    }

    match = FLIGHT_NO_RE.search(summary)
    if match:
        result["flight_no"] = match.group(1).replace(" ", "")

    parts = ARROW_RE.split(summary, maxsplit=1)
    if len(parts) == 2:
        for side, prefix in ((parts[0], "dep"), (parts[1], "arr")):
            airport = AIRPORT_RE.search(side)
            if not airport:
                continue
            city = airport.group(1)
            if result["flight_no"]:
                city = city.replace(result["flight_no"], "")
            city = _strip_edges(city)
            result[f"{prefix}_city"] = city or None
            result[f"{prefix}_code"] = airport.group(2)
    return result


def parse_aircraft(description: str) -> str | None:
    match = AIRCRAFT_RE.search(description or "")
    return match.group(1) if match else None


# --------------------------------------------------------------------------
# Экипаж и ФИО владельца календаря
# --------------------------------------------------------------------------

POSITIONS = ("КВС", "2П", "СБ", "БП", "ИНС", "ПИ", "СБЭ", "PIC", "FO", "SCCM", "CCM")
_POSITIONS_RE = "|".join(POSITIONS)

# ФИО приходят латиницей, должность — кириллицей. Отчество опционально.
# Требование явной должности в скобках защищает от ложных срабатываний
# на "Sheremetyevo (B)" и "Cairo (CAI | Cairo Intl)".
_NAME_PART = r"[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё'\-]+"
CREW_RE = re.compile(
    rf"({_NAME_PART})\s+({_NAME_PART})(?:\s+({_NAME_PART}))?"
    rf"\s*\(\s*({_POSITIONS_RE})\s*\)"
)


def parse_crew(description: str) -> list[tuple[str, str, str, str]]:
    """Возвращает список (фамилия, имя, отчество, должность)."""
    return [
        (last, first, middle or "", position)
        for last, first, middle, position in CREW_RE.findall(description or "")
    ]


def format_crew(description: str) -> list[str]:
    return [
        f"{last} {first}{' ' + middle if middle else ''} ({position})"
        for last, first, middle, position in parse_crew(description)
    ]


# Названия календарей, которые заведомо не являются ФИО.
GENERIC_CALENDAR_NAMES = ("schedule", "расписание", "calendar", "календарь", "aeroflot")


def detect_owner(calendar: Calendar, events: list[ParsedEvent]) -> tuple[str, str, str] | None:
    """ФИО владельца календаря.

    Провайдер отдаёт в X-WR-CALNAME название расписания, а не имя, поэтому
    основной способ здесь другой: владелец фигурирует в экипаже каждого
    своего рейса, остальные — лишь в части. Берём самого частого, но только
    если он встретился минимум дважды.
    """
    raw_name = calendar.get("X-WR-CALNAME")
    if raw_name:
        text = str(raw_name)
        if not any(word in text.lower() for word in GENERIC_CALENDAR_NAMES):
            parsed = _parse_full_name(text)
            if parsed:
                return parsed

    counter: Counter[tuple[str, str, str]] = Counter()
    events_with_crew = 0
    for event in events:
        crew = parse_crew(event.description)
        if crew:
            events_with_crew += 1
        for last, first, middle, _position in crew:
            counter[(last, first, middle)] += 1

    if not counter or events_with_crew < 2:
        return None

    (names, count), = counter.most_common(1)
    if count < 2:
        return None
    return names


NAME_RE = re.compile(rf"({_NAME_PART})\s+({_NAME_PART})(?:\s+({_NAME_PART}))?")


def _parse_full_name(text: str) -> tuple[str, str, str] | None:
    match = NAME_RE.search(text)
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3) or ""


# --------------------------------------------------------------------------
# Основная точка входа
# --------------------------------------------------------------------------


def parse_feed(
    raw_ics: str, fallback_tz: ZoneInfo
) -> tuple[list[ParsedEvent], tuple[str, str, str] | None]:
    """Разбирает ленту. Возвращает (события, ФИО владельца или None).

    Событие без UID или DTSTART пропускается с записью в лог, но не роняет
    разбор всей ленты.
    """
    try:
        calendar = Calendar.from_ical(raw_ics)
    except Exception as exc:  # noqa: BLE001 — icalendar кидает что угодно
        raise ParseError(f"Не удалось разобрать iCalendar: {exc}") from exc

    events: list[ParsedEvent] = []
    skipped = 0

    for component in calendar.walk("VEVENT"):
        parsed = _parse_component(component, fallback_tz)
        if parsed is None:
            skipped += 1
            continue
        events.append(parsed)

    if skipped:
        logger.warning("Пропущено некорректных событий: %s", skipped)

    # Дубли UID внутри одной ленты: оставляем последний.
    deduped: dict[str, ParsedEvent] = {}
    for event in events:
        deduped[event.uid] = event
    result = sorted(deduped.values(), key=lambda e: e.dtstart)

    return result, detect_owner(calendar, result)


def _parse_component(component, fallback_tz: ZoneInfo) -> ParsedEvent | None:
    uid = component.get("UID")
    dtstart_prop = component.get("DTSTART")
    if uid is None or dtstart_prop is None:
        return None

    dtstart = _to_aware(dtstart_prop.dt, fallback_tz)
    if dtstart is None:
        return None

    dtend_prop = component.get("DTEND")
    if dtend_prop is not None:
        dtend = _to_aware(dtend_prop.dt, fallback_tz)
    else:
        duration = component.get("DURATION")
        dtend = dtstart + duration.dt if duration is not None else dtstart
    if dtend is None:
        dtend = dtstart

    # Календари изредка отдают конец раньше начала. Не даём отрицательному
    # интервалу попасть в налёт.
    if dtend < dtstart:
        logger.warning("Событие %s: конец раньше начала, длительность обнулена", uid)
        dtend = dtstart

    raw_summary = str(component.get("SUMMARY") or "")
    description = str(component.get("DESCRIPTION") or "").strip()
    location = str(component.get("LOCATION") or "").strip() or None

    # Классифицируем по сырой строке: эмодзи-маркер стоит первым символом
    # и был бы срезан очисткой.
    kind = classify(raw_summary)
    summary = _tidy_summary(raw_summary)

    route: dict[str, str | None] = {}
    aircraft = None
    if kind is EventKind.FLIGHT:
        route = parse_flight_route(summary)
        aircraft = parse_aircraft(description)

    return ParsedEvent(
        uid=str(uid),
        kind=kind,
        summary=summary,
        description=description,
        dtstart=dtstart,
        dtend=dtend,
        location=location,
        flight_no=route.get("flight_no"),
        dep_code=route.get("dep_code"),
        dep_city=route.get("dep_city"),
        arr_code=route.get("arr_code"),
        arr_city=route.get("arr_city"),
        aircraft=aircraft,
        crew=format_crew(description),
    )


def _to_aware(value: object, fallback_tz: ZoneInfo) -> datetime | None:
    """DATE превращаем в полночь, naive DATETIME — привязываем к fallback_tz."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=fallback_tz)
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=fallback_tz)
    return None


_WS_RE = re.compile(r"[ \t]+")
# Ведущий и хвостовой мусор: эмодзи, дефисы, вертикальные черты, пробелы.
# \w в Python юникодный, поэтому кириллица и латиница сохраняются.
_EDGE_RE = re.compile(r"^[^\w(]+|[^\w)]+$")


def _tidy_summary(text: str) -> str:
    """Нормализует пробелы, но СОХРАНЯЕТ переносы строк.

    SUMMARY бывает двухчастным: первая строка — заголовок для списков
    и кнопок, остальное — подробности.
    """
    lines = [_WS_RE.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _strip_edges(text: str) -> str:
    return _EDGE_RE.sub("", _WS_RE.sub(" ", text).strip())


# --------------------------------------------------------------------------
# Границы периодов
# --------------------------------------------------------------------------


def month_bounds(moment: datetime, tz: ZoneInfo, offset: int = 0) -> tuple[datetime, datetime]:
    """Начало и конец месяца, сдвинутого на ``offset`` месяцев.

    Границы считаются с учётом года, а не только номера месяца — старая
    версия сравнивала месяцы числами и путала декабрь с январём.
    """
    local = moment.astimezone(tz)
    year = local.year
    month = local.month + offset
    year += (month - 1) // 12
    month = (month - 1) % 12 + 1

    start = datetime(year, month, 1, tzinfo=tz)
    next_month_start = datetime(
        year + (1 if month == 12 else 0),
        1 if month == 12 else month + 1,
        1,
        tzinfo=tz,
    )
    return start, next_month_start


def day_bounds(moment: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    local = moment.astimezone(tz)
    start = datetime(local.year, local.month, local.day, tzinfo=tz)
    return start, start + timedelta(days=1)


__all__ = [
    "ParseError",
    "classify",
    "day_bounds",
    "format_crew",
    "month_bounds",
    "parse_aircraft",
    "parse_crew",
    "parse_feed",
    "parse_flight_route",
]
