"""Расчёт ночного налёта.

Определение подобрано не из справочника, а по 1465 записям самой книжки:
солнце ниже −6° (гражданские сумерки), положение считается вдоль ортодромии
поминутно. С 2022 года расхождение с LogTen меньше минуты.

На ранних годах расхождение доходило до получаса — там время ставилось
вручную и, судя по всему, не всегда в UTC. Поэтому автоматический расчёт
применяется только к новым рейсам; перенесённая история остаётся как есть.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

# Порог гражданских сумерек. Проверено на реальных данных: −6.5° даёт чуть
# больше попаданий в пятиминутный допуск, но −6.0° точнее по медиане
# и совпадает со стандартным определением.
CIVIL_TWILIGHT_DEG = -6.0

# Шаг дискретизации. Минуты достаточно: результат и так округляется до минут.
STEP_MINUTES = 1


def sun_elevation(when: datetime, lat: float, lon: float) -> float:
    """Высота солнца над горизонтом в градусах, алгоритм NOAA."""
    julian_day = when.timestamp() / 86400.0 + 2440587.5
    t = (julian_day - 2451545.0) / 36525.0

    mean_long = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360
    mean_anomaly = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    anomaly_rad = math.radians(mean_anomaly)

    centre = (
        math.sin(anomaly_rad) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + math.sin(2 * anomaly_rad) * (0.019993 - 0.000101 * t)
        + math.sin(3 * anomaly_rad) * 0.000289
    )
    true_long = mean_long + centre
    omega = 125.04 - 1934.136 * t
    apparent_long = true_long - 0.00569 - 0.00478 * math.sin(math.radians(omega))

    seconds = 21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))
    obliquity = 23.0 + (26.0 + seconds / 60.0) / 60.0
    obliquity += 0.00256 * math.cos(math.radians(omega))

    declination = math.asin(
        math.sin(math.radians(obliquity)) * math.sin(math.radians(apparent_long))
    )

    y = math.tan(math.radians(obliquity / 2)) ** 2
    long_rad = math.radians(mean_long)
    eccentricity = 0.016708634
    equation_of_time = 4 * math.degrees(
        y * math.sin(2 * long_rad)
        - 2 * eccentricity * math.sin(anomaly_rad)
        + 4 * eccentricity * y * math.sin(anomaly_rad) * math.cos(2 * long_rad)
        - 0.5 * y * y * math.sin(4 * long_rad)
        - 1.25 * eccentricity**2 * math.sin(2 * anomaly_rad)
    )

    minutes = when.hour * 60 + when.minute + when.second / 60.0
    true_solar_time = (minutes + equation_of_time + 4 * lon) % 1440
    quarter = true_solar_time / 4
    hour_angle = math.radians(quarter - 180 if quarter >= 0 else quarter + 180)

    lat_rad = math.radians(lat)
    cosine = (
        math.sin(lat_rad) * math.sin(declination)
        + math.cos(lat_rad) * math.cos(declination) * math.cos(hour_angle)
    )
    zenith = math.acos(max(-1.0, min(1.0, cosine)))
    return 90.0 - math.degrees(zenith)


def great_circle_point(
    lat1: float, lon1: float, lat2: float, lon2: float, fraction: float
) -> tuple[float, float]:
    """Точка на ортодромии между двумя аэропортами."""
    p1, l1, p2, l2 = map(math.radians, (lat1, lon1, lat2, lon2))
    distance = 2 * math.asin(
        math.sqrt(
            math.sin((p2 - p1) / 2) ** 2
            + math.cos(p1) * math.cos(p2) * math.sin((l2 - l1) / 2) ** 2
        )
    )
    if distance < 1e-9:
        return lat1, lon1
    a = math.sin((1 - fraction) * distance) / math.sin(distance)
    b = math.sin(fraction * distance) / math.sin(distance)
    x = a * math.cos(p1) * math.cos(l1) + b * math.cos(p2) * math.cos(l2)
    y = a * math.cos(p1) * math.sin(l1) + b * math.cos(p2) * math.sin(l2)
    z = a * math.sin(p1) + b * math.sin(p2)
    return math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))


def night_minutes(
    out_utc: datetime,
    in_utc: datetime,
    dep: tuple[float, float] | None,
    arr: tuple[float, float] | None,
    threshold: float = CIVIL_TWILIGHT_DEG,
) -> int | None:
    """Ночное время рейса в минутах.

    Возвращает None, если координат хотя бы одного аэропорта нет: выдать
    ноль означало бы утверждать, что рейс был дневным, а это не установлено.
    """
    if dep is None or arr is None:
        return None
    if out_utc.tzinfo is None or in_utc.tzinfo is None:
        raise ValueError("отметки времени должны быть timezone-aware")

    total = int((in_utc - out_utc).total_seconds() // 60)
    if total <= 0:
        return 0

    count = 0
    for step in range(0, total, STEP_MINUTES):
        moment = out_utc + timedelta(minutes=step)
        lat, lon = great_circle_point(dep[0], dep[1], arr[0], arr[1], step / total)
        if sun_elevation(moment, lat, lon) < threshold:
            count += min(STEP_MINUTES, total - step)
    return count


def is_night_at(moment: datetime, lat: float, lon: float) -> bool:
    """Ночь ли в точке — для отметки ночных взлётов и посадок."""
    return sun_elevation(moment, lat, lon) < CIVIL_TWILIGHT_DEG
