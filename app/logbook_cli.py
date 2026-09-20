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

from app.db.logbook_repo import normalize_flight_number
from app.db.models import (
    Aircraft,
    Airport,
    Flight,
    FlightCrew,
    FlightSource,
    Function,
    Person,
    PersonAlias,
)
from app.logbook.translit import is_cyrillic, person_key, same_script, to_display_latin
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


def cmd_dedup(args) -> int:
    """Убирает дубли, возникшие из-за ошибки в поиске незаписанных рейсов.

    Дублем считается совпадение даты, маршрута, номера рейса и блок-тайма.
    Оставляется запись, созданная раньше — это перенесённая из LogTen,
    у неё заполнены поля, которых бот не собирает.
    """
    engine = _engine(args)
    with Session(engine) as session:
        flights = list(
            session.scalars(
                select(Flight).where(Flight.pilot_id == args.pilot_id).order_by(Flight.id)
            ).all()
        )
        # Две записи из LogTen дублями быть не могут: импорт идемпотентен,
        # значит это два разных рейса. За день по одному маршруту бывает
        # две ротации — 23.12.2016 в книжке как раз такой случай.
        # Дубль — только пара "перенесённая + заведённая ботом".
        seen: dict[tuple, Flight] = {}
        doomed: list[Flight] = []
        for flight in flights:
            key = (
                flight.flight_date,
                (flight.dep_icao or "").upper(),
                (flight.arr_icao or "").upper(),
                normalize_flight_number(flight.flight_number),
            )
            previous = seen.get(key)
            if previous is None:
                seen[key] = flight
                continue
            if previous.source is FlightSource.LOGTEN and flight.source is not FlightSource.LOGTEN:
                doomed.append(flight)
            elif flight.source is FlightSource.LOGTEN and previous.source is not FlightSource.LOGTEN:
                doomed.append(previous)
                seen[key] = flight
            # Обе из LogTen или обе заведены вручную — не трогаем.

        if not doomed:
            print("Дублей не найдено.")
            return 0

        print(f"Найдено дублей: {len(doomed)}")
        for flight in doomed:
            kept = seen[
                (
                    flight.flight_date,
                    (flight.dep_icao or "").upper(),
                    (flight.arr_icao or "").upper(),
                    normalize_flight_number(flight.flight_number),
                )
            ]
            print(
                f"  {flight.flight_date} {flight.dep_icao}->{flight.arr_icao} "
                f"{fmt_minutes(flight.block_minutes)}  "
                f"удалить id={flight.id} ({flight.source.value}), "
                f"оставить id={kept.id} ({kept.source.value})"
            )

        if not args.commit:
            print()
            print("Ничего не удалено. Добавьте --commit, чтобы применить.")
            return 2

        for flight in doomed:
            session.delete(flight)
        session.commit()
        print(f"\nУдалено: {len(doomed)}")
    return cmd_verify(args)


def cmd_merge_people(args) -> int:
    """Сливает карточки одного человека, заведённые разными алфавитами.

    Лента отдаёт ФИО кириллицей, книжка из LogTen хранит латиницу —
    до появления сопоставления по транслитерации бот заводил вторую
    карточку. Здесь такие пары находятся и объединяются.

    Остаётся запись с меньшим id: она из LogTen, на ней висит история
    и пометка владельца книжки. Связи с рейсами переносятся на неё,
    написания добавляются в алиасы, дубль удаляется.
    """
    engine = _engine(args)
    with Session(engine) as session:
        people = list(
            session.scalars(
                select(Person).where(Person.pilot_id == args.pilot_id).order_by(Person.id)
            ).all()
        )

        groups: dict[str, list[Person]] = {}
        for person in people:
            groups.setdefault(person_key(person.last_name, person.first_name), []).append(person)

        pairs = [(g[0], extra) for g in groups.values() if len(g) > 1 for extra in g[1:]]
        if not pairs:
            print("Одинаковых людей не найдено.")
            return 0

        print(f"Найдено пар: {len(pairs)}")
        for keep, drop in pairs:
            owner = "  <- владелец книжки" if keep.is_owner or drop.is_owner else ""
            print(f"  оставить id={keep.id} {keep.display}"
                  f"  <-  удалить id={drop.id} {drop.display}{owner}")

        if not args.commit:
            print()
            print("Ничего не изменено. Добавьте --commit, чтобы применить.")
            return 2

        moved = 0
        for keep, drop in pairs:
            # Переносим связи с рейсами, пропуская те, что уже есть.
            links = list(
                session.scalars(
                    select(FlightCrew).where(FlightCrew.person_id == drop.id)
                ).all()
            )
            existing = {
                (link.flight_id, link.role)
                for link in session.scalars(
                    select(FlightCrew).where(FlightCrew.person_id == keep.id)
                ).all()
            }
            for link in links:
                if (link.flight_id, link.role) in existing:
                    session.delete(link)
                else:
                    link.person_id = keep.id
                    moved += 1

            known = {a.alias for a in keep.aliases}
            for alias in drop.aliases:
                if alias.alias not in known:
                    keep.aliases.append(PersonAlias(alias=alias.alias))
                    known.add(alias.alias)

            # Отчество переносим только если алфавит совпадает, иначе
            # выйдет "Evstifeev Andrey Валерьевич".
            if (
                not keep.middle_name
                and drop.middle_name
                and same_script(keep.last_name, drop.middle_name)
            ):
                keep.middle_name = drop.middle_name
            if drop.is_owner:
                keep.is_owner = True
            if not keep.photo_file_id and drop.photo_file_id:
                keep.photo_file_id = drop.photo_file_id

            session.flush()
            session.delete(drop)

        session.commit()
        print(f"\nОбъединено пар: {len(pairs)}, перенесено связей с рейсами: {moved}")

        total = session.scalar(
            select(sqlfunc.count(Person.id)).where(Person.pilot_id == args.pilot_id)
        )
        owner = session.scalars(
            select(Person).where(
                Person.pilot_id == args.pilot_id, Person.is_owner.is_(True)
            )
        ).first()
        print(f"Людей в справочнике: {total}")
        print(f"Владелец книжки: {owner.display if owner else 'не определён'}")
        if owner:
            print(f"  написания: {', '.join(a.alias for a in owner.aliases)}")
    return 0


def cmd_latinize(args) -> int:
    """Переводит кириллические карточки людей в латиницу.

    Лента расписания отдаёт ФИО кириллицей, а отчёты уходят в зарубежные
    авиакомпании — там её не прочитают. Прежнее написание не теряется:
    оно остаётся в алиасах, и поиск работает по обоим вариантам.
    """
    engine = _engine(args)
    with Session(engine) as session:
        people = list(
            session.scalars(
                select(Person).where(Person.pilot_id == args.pilot_id).order_by(Person.id)
            ).all()
        )
        targets = [p for p in people if is_cyrillic(p.last_name)]
        if not targets:
            print("Кириллических карточек нет.")
            return 0

        print(f"Найдено: {len(targets)}")
        for person in targets:
            latin = " ".join(
                x
                for x in (
                    to_display_latin(person.last_name),
                    to_display_latin(person.first_name),
                    to_display_latin(person.middle_name),
                )
                if x
            )
            print(f"  id={person.id}  {person.display}  ->  {latin}")

        if not args.commit:
            print()
            print("Ничего не изменено. Добавьте --commit, чтобы применить.")
            return 2

        for person in targets:
            original = person.display
            known = {a.alias for a in person.aliases}
            if original not in known:
                person.aliases.append(PersonAlias(alias=original))
                known.add(original)

            person.last_name = to_display_latin(person.last_name)
            person.first_name = to_display_latin(person.first_name)
            person.middle_name = to_display_latin(person.middle_name)

            if person.display not in known:
                person.aliases.append(PersonAlias(alias=person.display))

        session.commit()
        print(f"\nПереведено: {len(targets)}")
        print("Прежние написания сохранены в алиасах.")
    return 0


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

    p = sub.add_parser("dedup", help="убрать задвоенные записи")
    p.add_argument("--pilot-id", type=int, required=True)
    p.add_argument("--commit", action="store_true")
    p.set_defaults(func=cmd_dedup)

    p = sub.add_parser("merge-people", help="склеить карточки одного человека")
    p.add_argument("--pilot-id", type=int, required=True)
    p.add_argument("--commit", action="store_true")
    p.set_defaults(func=cmd_merge_people)

    p = sub.add_parser("latinize", help="перевести кириллические ФИО в латиницу")
    p.add_argument("--pilot-id", type=int, required=True)
    p.add_argument("--commit", action="store_true")
    p.set_defaults(func=cmd_latinize)

    args = parser.parse_args(argv)
    if not args.database_url:
        print("Не задан DATABASE_URL.")
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
