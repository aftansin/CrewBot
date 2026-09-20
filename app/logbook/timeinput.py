"""Разбор и проверка введённых времён.

Пилот присылает одну строку: "0952 1537". Это единственное, что он
печатает после каждого рейса, поэтому разбор терпим к форматам,
а проверки — строгие.

Строгость нужна вот почему: ошибка здесь попадает в лётную книжку и
выглядит правдоподобно. Рейс просто окажется не в то время, и заметить
это через год будет нечем. Поэтому всё сомнительное либо отклоняется,
либо показывается пилоту до сохранения.

Главная ловушка — часовой пояс. Расписание приходит в московском времени,
а книжка ведётся в UTC. Разница ровно три часа, и набранное по привычке
московское время даёт внешне нормальную запись. Такой случай
распознаётся отдельно.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

# Максимальная длительность, за которой значение заведомо ошибочно.
# Порог взят не на глаз: самый длинный рейс за пятнадцать лет книжки —
# 7:24, медиана 3:15. Двенадцать часов вдвое выше любого реального рейса,
# и при этом ловит перепутанные местами времена: перестановка всегда
# даёт 24 часа минус настоящая длительность, то есть больше шестнадцати.
MAX_BLOCK_MINUTES = 12 * 60
# Ниже этого рейс подозрителен, но возможен: возврат на вылет.
SHORT_BLOCK_MINUTES = 15
# Сдвиг, указывающий на московское время вместо UTC.
MOSCOW_OFFSET_MINUTES = 180
OFFSET_TOLERANCE_MINUTES = 12
# Расхождение с планом, после которого вероятна ошибка в дате.
MAX_PLAN_DEVIATION_MINUTES = 12 * 60

_SEPARATORS = re.compile(r"[\s,;/\-–—]+")
_TIME = re.compile(r"^(?:([01]?\d|2[0-3]):?([0-5]\d))$")


class Severity:
    ERROR = "error"      # сохранять нельзя
    WARNING = "warning"  # сохранить можно, но пилот должен подтвердить


@dataclass(slots=True)
class Issue:
    severity: str
    message: str
    hint: str | None = None


@dataclass(slots=True)
class ParsedTimes:
    out: time | None = None
    inn: time | None = None
    issues: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            self.out is not None
            and self.inn is not None
            and not any(i.severity == Severity.ERROR for i in self.issues)
        )

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == Severity.ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == Severity.WARNING]


def parse_times(text: str) -> ParsedTimes:
    """Принимает "0952 1537", "09:52 15:37", "0952-1537" и подобное."""
    result = ParsedTimes()
    parts = [p for p in _SEPARATORS.split((text or "").strip()) if p]

    if len(parts) != 2:
        result.issues.append(
            Issue(
                Severity.ERROR,
                "Нужно два времени: запуск и выключение.",
                "Например: 0952 1537",
            )
        )
        return result

    parsed: list[time] = []
    for raw in parts:
        match = _TIME.match(raw)
        if not match:
            result.issues.append(
                Issue(
                    Severity.ERROR,
                    f"Не удалось разобрать время: {raw}",
                    "Четыре цифры или часы:минуты, например 0952 или 09:52",
                )
            )
            return result
        parsed.append(time(int(match.group(1)), int(match.group(2))))

    result.out, result.inn = parsed
    return result


def build_datetimes(
    flight_date: datetime, out: time, inn: time
) -> tuple[datetime, datetime]:
    """Собирает отметки, разворачивая переход через полночь."""
    tz = flight_date.tzinfo
    day = flight_date.date()
    out_dt = datetime(day.year, day.month, day.day, out.hour, out.minute, tzinfo=tz)
    in_dt = datetime(day.year, day.month, day.day, inn.hour, inn.minute, tzinfo=tz)
    if in_dt <= out_dt:
        in_dt += timedelta(days=1)
    return out_dt, in_dt


def validate(
    out_dt: datetime,
    in_dt: datetime,
    scheduled_out: datetime | None = None,
    scheduled_in: datetime | None = None,
) -> list[Issue]:
    """Проверки собранных отметок против здравого смысла и плана."""
    issues: list[Issue] = []
    block = int((in_dt - out_dt).total_seconds() // 60)

    if block <= 0:
        issues.append(Issue(Severity.ERROR, "Длительность рейса получилась нулевой."))
        return issues

    if block > MAX_BLOCK_MINUTES:
        issues.append(
            Issue(
                Severity.ERROR,
                f"Длительность {block // 60} ч {block % 60:02d} мин — это слишком много.",
                "Похоже, запуск и выключение перепутаны местами.",
            )
        )
        return issues

    if block < SHORT_BLOCK_MINUTES:
        issues.append(
            Issue(
                Severity.WARNING,
                f"Очень короткий рейс: {block} мин.",
                "Если это возврат на вылет — всё верно, подтвердите.",
            )
        )

    if scheduled_out is None:
        return issues

    deviation = int((out_dt - scheduled_out).total_seconds() // 60)

    # Три часа ровно — это московское время вместо UTC, а не задержка.
    if abs(abs(deviation) - MOSCOW_OFFSET_MINUTES) <= OFFSET_TOLERANCE_MINUTES:
        direction = "позже" if deviation > 0 else "раньше"
        issues.append(
            Issue(
                Severity.WARNING,
                f"Время отличается от планового ровно на три часа ({direction}).",
                "Похоже на московское время вместо UTC. "
                "В книжке время ведётся в UTC — проверьте.",
            )
        )
    elif abs(deviation) > MAX_PLAN_DEVIATION_MINUTES:
        issues.append(
            Issue(
                Severity.ERROR,
                f"Запуск отличается от планового на {abs(deviation) // 60} ч.",
                "Возможно, рейс относится к другой дате.",
            )
        )
    elif abs(deviation) > 240:
        issues.append(
            Issue(
                Severity.WARNING,
                f"Задержка {deviation // 60} ч {abs(deviation) % 60:02d} мин относительно плана.",
            )
        )

    if scheduled_in is not None:
        planned_block = int((scheduled_in - scheduled_out).total_seconds() // 60)
        if planned_block > 0:
            difference = block - planned_block
            if abs(difference) > max(90, planned_block // 2):
                sign = "дольше" if difference > 0 else "короче"
                issues.append(
                    Issue(
                        Severity.WARNING,
                        f"Рейс на {abs(difference) // 60} ч {abs(difference) % 60:02d} мин "
                        f"{sign} планового.",
                        "Если был уход на запасной или возврат — отметьте это.",
                    )
                )

    return issues


def parse_and_validate(
    text: str,
    flight_date: datetime,
    scheduled_out: datetime | None = None,
    scheduled_in: datetime | None = None,
) -> tuple[datetime | None, datetime | None, list[Issue]]:
    """Полный путь от строки пилота до пары отметок с замечаниями."""
    parsed = parse_times(text)
    if parsed.out is None or parsed.inn is None:
        return None, None, parsed.issues

    out_dt, in_dt = build_datetimes(flight_date, parsed.out, parsed.inn)
    issues = parsed.issues + validate(out_dt, in_dt, scheduled_out, scheduled_in)

    if any(i.severity == Severity.ERROR for i in issues):
        return None, None, issues
    return out_dt, in_dt, issues


# --------------------------------------------------------------------------
# Ручной ввод рейса
# --------------------------------------------------------------------------

_ICAO = re.compile(r"^[A-Z]{4}$")
_DATE_FORMATS = ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y")


@dataclass(slots=True)
class ManualFlight:
    flight_date: date | None = None
    dep: str | None = None
    arr: str | None = None
    out: time | None = None
    inn: time | None = None
    issues: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            self.flight_date is not None
            and self.dep is not None
            and self.arr is not None
            and self.out is not None
            and self.inn is not None
            and not any(i.severity == Severity.ERROR for i in self.issues)
        )


def parse_manual_flight(text: str, today: date) -> ManualFlight:
    """Разбирает строку "14.09.2026 UUEE UIII 2238 0401".

    Пять значений: дата, откуда, куда, запуск, выключение. Порядок
    фиксированный — угадывать, где тут что, значит однажды угадать неверно
    и записать рейс задом наперёд.
    """
    result = ManualFlight()
    parts = [p for p in _SEPARATORS.split((text or "").strip()) if p]

    if len(parts) != 5:
        result.issues.append(
            Issue(
                Severity.ERROR,
                "Нужно пять значений: дата, откуда, куда, запуск, выключение.",
                "Например: 14.09.2026 UUEE UIII 2238 0401",
            )
        )
        return result

    raw_date, raw_dep, raw_arr, raw_out, raw_in = parts

    for fmt in _DATE_FORMATS:
        try:
            result.flight_date = datetime.strptime(raw_date, fmt).date()
            break
        except ValueError:
            continue
    if result.flight_date is None:
        result.issues.append(
            Issue(Severity.ERROR, f"Не похоже на дату: {raw_date}", "Например: 14.09.2026")
        )
        return result

    if result.flight_date > today:
        result.issues.append(
            Issue(
                Severity.ERROR,
                "Дата в будущем.",
                "В лётную книжку записывают то, что уже выполнено.",
            )
        )
        return result

    for raw, label in ((raw_dep, "вылета"), (raw_arr, "прилёта")):
        if not _ICAO.match(raw.upper()):
            result.issues.append(
                Issue(
                    Severity.ERROR,
                    f"Код аэропорта {label} должен быть из четырёх букв: {raw}",
                    "Это ICAO, например UUEE или LTAI.",
                )
            )
            return result
    result.dep, result.arr = raw_dep.upper(), raw_arr.upper()

    times = parse_times(f"{raw_out} {raw_in}")
    if times.out is None or times.inn is None:
        result.issues.extend(times.issues)
        return result
    result.out, result.inn = times.out, times.inn

    return result
