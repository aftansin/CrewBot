"""Перенос лётной книжки в базу бота.

Импорт идёт из исходной выгрузки LogTen, а не из базы flightlog. Схемы
разошлись — у рейсов и людей появилась привязка к пилоту, — и переливка
из базы в базу была бы хрупкой. Повторный разбор исходника надёжнее:
у него встроена сверка, и он идемпотентен.

    python -m app.logbook_cli report  /data/export.txt
    python -m app.logbook_cli import  /data/export.txt --pilot-id 438391677 --commit
    python -m app.logbook_cli verify  --pilot-id 438391677
"""

from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import create_engine, select
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from app.db.models import Aircraft, Airport, Flight, Function, Person
from app.logten import report as report_module
from app.logten.importer import Importer
from app.logten.reader import Reader
from app.logten.values import fmt_minutes

EXPECTED_RECORDS = 3117
EXPECTED_TOTAL = "10576:54"


def sync_url(url: str) -> str:
    """Бот ходит в базу асинхронно, импортёр — обычным драйвером."""
    return url.replace("+asyncpg", "+psycopg").replace(
        "postgresql://", "postgresql+psycopg://"
    )


def _engine(args):
    return create_engine(sync_url(args.database_url))


def cmd_report(args) -> int:
    reader = Reader(args.export)
    records = reader.read()
    summary = report_module.build(records)
    expected = [
        ("записей", EXPECTED_RECORDS, summary.records),
        ("суммарный налёт", EXPECTED_TOTAL, fmt_minutes(summary.total_minutes)),
    ]
    print(report_module.render(records, reader.problems, summary, expected))
    return 1 if reader.problems else 0


def cmd_import(args) -> int:
    if not args.commit:
        print("Запись не выполнена: добавьте --commit.")
        return 2

    reader = Reader(args.export)
    records = reader.read()
    if reader.problems and not args.force:
        print(f"Не разобрано значений: {len(reader.problems)}. Импорт остановлен.")
        return 1

    engine = _engine(args)
    with Session(engine) as session:
        # Пилот должен уже существовать: он заводится, когда пишет боту /start.
        existing = session.scalar(
            select(sqlfunc.count(Flight.id)).where(Flight.pilot_id == args.pilot_id)
        )
        if existing and not args.force:
            print(f"У пилота {args.pilot_id} уже есть {existing} рейсов.")
            print("Повторный импорт обновит их. Добавьте --force, если это и нужно.")
            return 1

        importer = Importer(session, pilot_id=args.pilot_id, owner_hint=args.owner)
        stats = importer.import_records(records, catalogue_path=args.catalogue)
        session.commit()

    print(f"рейсов создано  : {stats.flights_created}")
    print(f"рейсов обновлено: {stats.flights_updated}")
    print(f"людей           : {stats.people_created}")
    print(f"бортов          : {stats.aircraft_created}")
    print(f"аэропортов      : {stats.airports_created}")
    print(f"связей экипажа  : {stats.crew_links}")
    return cmd_verify(args)


def cmd_verify(args) -> int:
    engine = _engine(args)
    with Session(engine) as session:
        flights = list(
            session.scalars(select(Flight).where(Flight.pilot_id == args.pilot_id)).all()
        )
        if not flights:
            print(f"У пилота {args.pilot_id} рейсов нет.")
            return 1

        total = sum(f.block_minutes for f in flights)
        pic = sum(f.easa_pic_minutes for f in flights)
        copilot = sum(f.easa_copilot_minutes for f in flights)
        dual = sum(f.easa_dual_minutes for f in flights)
        unverified = sum(
            f.block_minutes for f in flights if f.function is Function.UNVERIFIED
        )
        people = session.scalar(
            select(sqlfunc.count(Person.id)).where(Person.pilot_id == args.pilot_id)
        )

        print()
        print("=" * 56)
        print(f"СВЕРКА КНИЖКИ ПИЛОТА {args.pilot_id}")
        print("=" * 56)
        print(f"  рейсов          {len(flights)}")
        print(f"  налёт           {fmt_minutes(total)}")
        print(f"  EASA PIC        {fmt_minutes(pic)}")
        print(f"  EASA второй п.  {fmt_minutes(copilot)}")
        print(f"  EASA dual       {fmt_minutes(dual)}")
        print(f"  не подтверждено {fmt_minutes(unverified)}")
        print(f"  людей           {people}")
        print(f"  бортов          {session.scalar(select(sqlfunc.count(Aircraft.id)))}")
        print(f"  аэропортов      {session.scalar(select(sqlfunc.count(Airport.icao)))}")

        ok = pic + copilot + dual + unverified == total
        print()
        print(f"  сумма функций   {fmt_minutes(pic + copilot + dual + unverified)}  "
              f"{'сходится' if ok else 'РАСХОЖДЕНИЕ'}")
        if len(flights) == EXPECTED_RECORDS and fmt_minutes(total) == EXPECTED_TOTAL:
            print("  контрольные цифры совпали")
        else:
            print(f"  ожидалось {EXPECTED_RECORDS} / {EXPECTED_TOTAL}")
            ok = False
        return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="logbook", description="Перенос книжки в базу бота")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument("--catalogue", default=os.environ.get("AIRPORTS_CSV", "/data/airports.csv"))
    parser.add_argument("--owner", default="Evstifeev")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("report", help="предпросмотр, в базу ничего не пишется")
    p.add_argument("export")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("import", help="запись в базу бота")
    p.add_argument("export")
    p.add_argument("--pilot-id", type=int, required=True, help="telegram id владельца книжки")
    p.add_argument("--commit", action="store_true")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("verify", help="сверка книжки в базе")
    p.add_argument("--pilot-id", type=int, required=True)
    p.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    if not args.database_url:
        print("Не задан DATABASE_URL.")
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
