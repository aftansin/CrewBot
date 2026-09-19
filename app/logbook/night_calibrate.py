"""Подбор формулы ночного налёта по фактическим записям логбука.

Идея: пройти рейс поминутно, интерполируя положение по ортодромии между
аэропортами, посчитать высоту солнца и сложить минуты, попадающие под
то или иное определение ночи. Затем сравнить с тем, что стоит в логбуке.
"""
from __future__ import annotations

import contextlib
import csv
import math
import re
from datetime import UTC, datetime, timedelta

AIRPORTS = "data/airports.csv"
EXPORT = "/mnt/user-data/uploads/Export_Flights__Tab__-_2026-09-19_06-50-05.txt"


def load_airports() -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    with open(AIRPORTS, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            for key in (row.get("icao_code"), row.get("ident"), row.get("gps_code")):
                if key and len(key) == 4:
                    with contextlib.suppress(TypeError, ValueError):
                        out.setdefault(key, (float(row["latitude_deg"]), float(row["longitude_deg"])))
    return out


def sun_elevation(when: datetime, lat: float, lon: float) -> float:
    """Высота солнца над горизонтом в градусах (алгоритм NOAA)."""
    jd = when.timestamp() / 86400.0 + 2440587.5
    t = (jd - 2451545.0) / 36525.0

    # Средняя долгота и аномалия
    l0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360
    m = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    mr = math.radians(m)

    # Уравнение центра -> истинная долгота
    c = (math.sin(mr) * (1.914602 - t * (0.004817 + 0.000014 * t))
         + math.sin(2 * mr) * (0.019993 - 0.000101 * t)
         + math.sin(3 * mr) * 0.000289)
    true_long = l0 + c
    omega = 125.04 - 1934.136 * t
    app_long = true_long - 0.00569 - 0.00478 * math.sin(math.radians(omega))

    # Наклон эклиптики
    seconds = 21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))
    e0 = 23.0 + (26.0 + seconds / 60.0) / 60.0
    e = e0 + 0.00256 * math.cos(math.radians(omega))

    decl = math.asin(math.sin(math.radians(e)) * math.sin(math.radians(app_long)))

    # Уравнение времени
    y = math.tan(math.radians(e / 2)) ** 2
    l0r = math.radians(l0)
    eot = 4 * math.degrees(
        y * math.sin(2 * l0r)
        - 2 * 0.016708634 * math.sin(mr)
        + 4 * 0.016708634 * y * math.sin(mr) * math.cos(2 * l0r)
        - 0.5 * y * y * math.sin(4 * l0r)
        - 1.25 * 0.016708634**2 * math.sin(2 * mr)
    )

    minutes = when.hour * 60 + when.minute + when.second / 60.0
    true_solar = (minutes + eot + 4 * lon) % 1440
    hour_angle = math.radians(true_solar / 4 - 180 if true_solar / 4 >= 0 else true_solar / 4 + 180)

    latr = math.radians(lat)
    zenith = math.acos(
        max(-1.0, min(1.0, math.sin(latr) * math.sin(decl)
                      + math.cos(latr) * math.cos(decl) * math.cos(hour_angle)))
    )
    return 90.0 - math.degrees(zenith)


def interpolate(lat1, lon1, lat2, lon2, fraction):
    """Точка на ортодромии между двумя аэропортами."""
    p1, l1, p2, l2 = map(math.radians, (lat1, lon1, lat2, lon2))
    d = 2 * math.asin(math.sqrt(
        math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin((l2 - l1) / 2) ** 2
    ))
    if d < 1e-9:
        return lat1, lon1
    a = math.sin((1 - fraction) * d) / math.sin(d)
    b = math.sin(fraction * d) / math.sin(d)
    x = a * math.cos(p1) * math.cos(l1) + b * math.cos(p2) * math.cos(l2)
    y = a * math.cos(p1) * math.sin(l1) + b * math.cos(p2) * math.sin(l2)
    z = a * math.sin(p1) + b * math.sin(p2)
    return math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))


def night_minutes(dep_dt, arr_dt, lat1, lon1, lat2, lon2, threshold, offset_min=0):
    """Минуты ночи. threshold — высота солнца в градусах.

    offset_min имитирует определение "через N минут после захода": точка
    считается ночной, если солнце было ниже порога N минут назад и остаётся
    ниже сейчас — грубо, но для сравнения определений достаточно.
    """
    total = int((arr_dt - dep_dt).total_seconds() // 60)
    if total <= 0:
        return 0
    count = 0
    for step in range(total):
        moment = dep_dt + timedelta(minutes=step)
        lat, lon = interpolate(lat1, lon1, lat2, lon2, step / total)
        elev = sun_elevation(moment, lat, lon)
        if offset_min:
            shifted = sun_elevation(moment - timedelta(minutes=offset_min), lat, lon)
            if elev < threshold and shifted < threshold:
                count += 1
        elif elev < threshold:
            count += 1
    return count


def to_minutes(text: str) -> int | None:
    m = re.match(r"^(\d+):(\d{2})$", text.strip())
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def main() -> None:
    airports = load_airports()
    with open(EXPORT, encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh, delimiter="\t"))
    hdr = [h.strip() for h in rows[0]]
    idx = {n: i for i, n in enumerate(hdr)}

    def g(r, n):
        i = idx.get(n)
        return r[i].strip() if i is not None and i < len(r) else ""

    # Берём записи, где ночное время проставлено — по ним и калибруем.
    samples = []
    missing_ap = set()
    for r in rows[1:]:
        night = to_minutes(g(r, "flight_night"))
        if night is None:
            continue
        fr, to = g(r, "flight_from"), g(r, "flight_to")
        if fr not in airports:
            missing_ap.add(fr)
            continue
        if to not in airports:
            missing_ap.add(to)
            continue
        dep, arr = g(r, "flight_actualDepartureTime"), g(r, "flight_actualArrivalTime")
        date_s = g(r, "flight_flightDate")
        if not (dep and arr and date_s):
            continue
        d = datetime.fromisoformat(date_s).date()
        dh, dm = map(int, dep.split(":"))
        ah, am = map(int, arr.split(":"))
        dep_dt = datetime(d.year, d.month, d.day, dh, dm, tzinfo=UTC)
        arr_dt = datetime(d.year, d.month, d.day, ah, am, tzinfo=UTC)
        if arr_dt <= dep_dt:
            arr_dt += timedelta(days=1)
        samples.append((dep_dt, arr_dt, airports[fr], airports[to], night,
                        to_minutes(g(r, "flight_totalTime")) or 0))

    print(f"записей для калибровки: {len(samples)}")
    if missing_ap:
        print(f"аэропортов нет в справочнике: {len(missing_ap)} -> {sorted(missing_ap)[:10]}")
    print()

    subset = samples[::4]  # каждая четвёртая, чтобы считалось за разумное время
    print(f"проверяю на выборке из {len(subset)} рейсов\n")

    variants = [
        ("солнце ниже 0° (геометрический заход)", 0.0, 0),
        ("солнце ниже -0.833° (с рефракцией)", -0.833, 0),
        ("солнце ниже -6° (гражданские сумерки, FAA)", -6.0, 0),
        ("солнце ниже -12° (навигационные сумерки)", -12.0, 0),
        ("-0.833° + 30 мин (EASA)", -0.833, 30),
    ]

    print(f"{'определение':46} {'сред.откл':>10} {'медиана':>9} {'точно':>7} {'±5мин':>7}")
    print("-" * 84)
    for name, thr, off in variants:
        diffs = []
        for dep_dt, arr_dt, a, b, night, _tot in subset:
            calc = night_minutes(dep_dt, arr_dt, a[0], a[1], b[0], b[1], thr, off)
            diffs.append(calc - night)
        n = len(diffs)
        mean = sum(abs(x) for x in diffs) / n
        med = sorted(abs(x) for x in diffs)[n // 2]
        exact = sum(1 for x in diffs if x == 0) * 100 // n
        near = sum(1 for x in diffs if abs(x) <= 5) * 100 // n
        print(f"{name:46} {mean:9.1f} {med:9} {exact:6}% {near:6}%")


if __name__ == "__main__":
    main()
