"""Отчёты в PDF.

Только английский и только латиница: отчёты уходят в зарубежные
авиакомпании, а заодно это избавляет от встраивания шрифта с кириллицей
в образ. Всё, что приходит из данных — имена, заметки, — прогоняется
через транслитерацию и очистку, иначе один русский символ в заметке
уронит генерацию.

Формы отчётов готовые, а не настраиваемые: конструктор в чате
превращается в десяток вопросов перед каждым файлом, а нужных форм
на деле четыре.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime

from fpdf import FPDF
from fpdf.enums import Align, XPos, YPos

from app.db.models import Flight, Function
from app.logbook.translit import to_display_latin

MONTHS = ("", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

PIC_FUNCTIONS = {Function.PIC, Function.PICUS, Function.FI, Function.FE}
COPILOT_FUNCTIONS = {Function.COPILOT, Function.CRUISE_RELIEF}

FUNCTION_SHORT = {
    Function.PIC: "PIC",
    Function.PICUS: "PICUS",
    Function.COPILOT: "SIC",
    Function.CRUISE_RELIEF: "CRZ",
    Function.DUAL: "DUAL",
    Function.FI: "FI",
    Function.FE: "FE",
    Function.UNVERIFIED: "-",
}


def short_name(full: str | None) -> str:
    """Фамилия и имя без отчества.

    В зарубежных лётных книжках отчества нет, и в отчёте оно только
    засоряет шапку.
    """
    parts = (latin1(full) or "").split()
    return " ".join(parts[:2])


def latin1(text: str | None) -> str:
    """Приводит к тому, что умеют встроенные шрифты.

    Кириллица транслитерируется, всё прочее непечатаемое отбрасывается:
    отчёт не должен падать из-за одного символа в заметке.
    """
    if not text:
        return ""
    value = to_display_latin(str(text)) or ""
    # Типографские символы тоже вне latin-1: длинное тире в подписи
    # роняло генерацию не хуже кириллицы.
    for source, target in (
        ("\u2014", "-"), ("\u2013", "-"), ("\u2212", "-"),
        ("\u201c", '"'), ("\u201d", '"'), ("\u00ab", '"'), ("\u00bb", '"'),
        ("\u2018", "'"), ("\u2019", "'"), ("\u2026", "..."), ("\u00a0", " "),
    ):
        value = value.replace(source, target)
    return value.encode("latin-1", errors="replace").decode("latin-1")


def hhmm(minutes: int | None) -> str:
    total = minutes or 0
    return f"{total // 60}:{total % 60:02d}"


class Totals:
    __slots__ = ("flights", "block", "pic", "sic", "night", "ldg_day", "ldg_night")

    def __init__(self) -> None:
        self.flights = self.block = self.pic = self.sic = 0
        self.night = self.ldg_day = self.ldg_night = 0

    def add(self, flight: Flight) -> None:
        # Пустые поля не должны ронять отчёт: одна незаполненная посадка
        # в тысяче рейсов иначе оставит без файла весь документ.
        block = flight.block_minutes or 0
        self.flights += 1
        self.block += block
        if flight.function in PIC_FUNCTIONS:
            self.pic += block
        elif flight.function in COPILOT_FUNCTIONS:
            self.sic += block
        self.night += flight.night_minutes or 0
        self.ldg_day += flight.day_landings or 0
        self.ldg_night += flight.night_landings or 0


class LogbookPDF(FPDF):
    def __init__(self, owner: str, subtitle: str, landscape: bool = False) -> None:
        super().__init__(orientation="L" if landscape else "P", unit="mm", format="A4")
        self.owner = short_name(owner)
        self.subtitle = latin1(subtitle)
        # Раздел (обычно год) показывается в шапке каждой страницы. Без
        # этого таблица, переехавшая на следующий лист, выглядит оторванной
        # от своего заголовка.
        self.section_label = ""
        self.zebra = False
        self.set_auto_page_break(auto=True, margin=15)
        self.set_title(f"Flight Log - {self.owner}")

    def header(self) -> None:
        self.set_font("Helvetica", "B", 13)
        self.cell(0, 7, "PILOT FLIGHT LOG", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_font("Helvetica", "", 10)
        self.cell(0, 5, self.owner, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_font("Helvetica", "I", 9)
        caption = self.subtitle
        if self.section_label:
            caption = f"{caption}   |   {self.section_label}"
        self.cell(0, 5, latin1(caption), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_draw_color(160, 160, 160)
        self.line(self.l_margin, self.get_y() + 1, self.w - self.r_margin, self.get_y() + 1)
        self.ln(4)

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(120, 120, 120)
        stamp = datetime.now().strftime("%d %b %Y")
        self.cell(0, 5, f"Generated {stamp}", align=Align.L)
        self.set_y(-12)
        self.cell(0, 5, f"Page {self.page_no()}/{{nb}}", align=Align.R)
        self.set_text_color(0, 0, 0)

    # -- вспомогательное ---------------------------------------------------

    def table_header(self, columns: list[tuple[str, int]]) -> None:
        self.set_font("Helvetica", "B", 8)
        self.set_fill_color(228, 230, 233)
        for title, width in columns:
            self.cell(width, 6.5, latin1(title), border=0, align=Align.C, fill=True)
        self.ln()
        self.set_font("Helvetica", "", 8)
        self.zebra = False

    def row(self, values: list[tuple[str, int, str]], bold: bool = False) -> None:
        self.set_font("Helvetica", "B" if bold else "", 8)
        # Чередование фона: на плотной таблице глаз иначе теряет строку.
        fill = not bold and self.zebra
        if fill:
            self.set_fill_color(246, 247, 248)
        for text, width, align in values:
            self.cell(
                width, 5.2, latin1(text), border="T" if bold else 0,
                align=align, fill=fill,
            )
        self.ln()
        self.zebra = not self.zebra

    def section(self, title: str) -> None:
        self.ln(2)
        self.set_font("Helvetica", "B", 10)
        self.cell(0, 6, latin1(title), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_font("Helvetica", "", 9)


def _totals_block(pdf: LogbookPDF, title: str, totals: Totals) -> None:
    pdf.section(title)
    pdf.set_font("Helvetica", "", 9)
    rows = [
        ("Flights", str(totals.flights)),
        ("Total time", hhmm(totals.block)),
        ("PIC", hhmm(totals.pic)),
        ("SIC", hhmm(totals.sic)),
        ("Night", hhmm(totals.night)),
        ("Landings (day / night)", f"{totals.ldg_day} / {totals.ldg_night}"),
    ]
    for label, value in rows:
        pdf.cell(60, 5, latin1(label))
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(30, 5, latin1(value), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", "", 9)


# --------------------------------------------------------------------------
# Формы отчётов
# --------------------------------------------------------------------------

# Ширины под альбомный A4: сумма 277 мм — вся полезная ширина листа
# за вычетом полей.
FLIGHT_COLUMNS = [
    ("DATE", 26), ("FLIGHT", 24), ("FROM", 22), ("TO", 22), ("AIRCRAFT", 34),
    ("OUT", 20), ("IN", 20), ("TOTAL", 25), ("PIC", 24), ("SIC", 24),
    ("NIGHT", 24), ("FUNC", 12),
]


def _flight_row(pdf: LogbookPDF, flight: Flight) -> None:
    tail = flight.aircraft.display if flight.aircraft else ""
    pic = hhmm(flight.block_minutes) if flight.function in PIC_FUNCTIONS else ""
    sic = hhmm(flight.block_minutes) if flight.function in COPILOT_FUNCTIONS else ""
    pdf.row([
        (flight.flight_date.strftime("%d %b %y"), 26, Align.L),
        (latin1(flight.flight_number), 24, Align.L),
        (flight.dep_icao or "", 22, Align.C),
        (flight.arr_icao or "", 22, Align.C),
        (latin1(tail), 34, Align.L),
        (flight.out_utc.strftime("%H:%M") if flight.out_utc else "", 20, Align.C),
        (flight.in_utc.strftime("%H:%M") if flight.in_utc else "", 20, Align.C),
        (hhmm(flight.block_minutes), 25, Align.R),
        (pic, 24, Align.R),
        (sic, 24, Align.R),
        (hhmm(flight.night_minutes) if flight.night_minutes else "", 24, Align.R),
        (FUNCTION_SHORT.get(flight.function, ""), 12, Align.C),
    ])


def _detail_pages(pdf: LogbookPDF, flights: list[Flight]) -> Totals:
    grand = Totals()
    pdf.table_header(FLIGHT_COLUMNS)
    for flight in sorted(flights, key=lambda f: (f.flight_date, f.out_utc or datetime.min)):
        if pdf.get_y() > pdf.h - 30:
            pdf.add_page()
            pdf.table_header(FLIGHT_COLUMNS)
        _flight_row(pdf, flight)
        grand.add(flight)

    total_width = sum(w for _, w in FLIGHT_COLUMNS[:7])
    pdf.row(
        [
            ("TOTAL", total_width, Align.R),
            (hhmm(grand.block), 25, Align.R),
            (hhmm(grand.pic), 24, Align.R),
            (hhmm(grand.sic), 24, Align.R),
            (hhmm(grand.night), 24, Align.R),
            ("", 12, Align.C),
        ],
        bold=True,
    )
    return grand


def build_summary(owner: str, flights: list[Flight]) -> bytes:
    """Одна страница: итоги по годам и за всю карьеру. Для резюме."""
    pdf = LogbookPDF(owner, "Summary of flight experience")
    pdf.alias_nb_pages()
    pdf.add_page()

    by_year: dict[int, Totals] = defaultdict(Totals)
    by_type: dict[str, Totals] = defaultdict(Totals)
    grand = Totals()
    for flight in flights:
        by_year[flight.flight_date.year].add(flight)
        grand.add(flight)
        kind = flight.aircraft.type_name if flight.aircraft else None
        by_type[latin1(kind) or "Unspecified"].add(flight)

    _totals_block(pdf, "Grand total", grand)

    pdf.section("By year")
    columns = [("YEAR", 20), ("FLIGHTS", 22), ("TOTAL", 22), ("PIC", 22),
               ("SIC", 22), ("NIGHT", 22)]
    pdf.table_header(columns)
    for year in sorted(by_year):
        totals = by_year[year]
        pdf.row([
            (str(year), 20, Align.L), (str(totals.flights), 22, Align.R),
            (hhmm(totals.block), 22, Align.R), (hhmm(totals.pic), 22, Align.R),
            (hhmm(totals.sic), 22, Align.R), (hhmm(totals.night), 22, Align.R),
        ])

    pdf.section("By aircraft type")
    columns = [("TYPE", 50), ("FLIGHTS", 22), ("TOTAL", 22), ("PIC", 22)]
    pdf.table_header(columns)
    for kind in sorted(by_type, key=lambda k: -by_type[k].block):
        totals = by_type[kind]
        pdf.row([
            (kind[:28], 50, Align.L), (str(totals.flights), 22, Align.R),
            (hhmm(totals.block), 22, Align.R), (hhmm(totals.pic), 22, Align.R),
        ])

    return bytes(pdf.output())


def build_year(owner: str, flights: list[Flight], year: int) -> bytes:
    """Один год: месяцы таблицей плюс итог."""
    pdf = LogbookPDF(owner, f"Year {year}")
    pdf.alias_nb_pages()
    pdf.add_page()

    by_month: dict[int, Totals] = defaultdict(Totals)
    grand = Totals()
    for flight in flights:
        by_month[flight.flight_date.month].add(flight)
        grand.add(flight)

    columns = [("MONTH", 24), ("FLIGHTS", 20), ("TOTAL", 22), ("PIC", 22),
               ("SIC", 22), ("NIGHT", 22), ("LDG", 18)]
    pdf.table_header(columns)
    for month in range(1, 13):
        totals = by_month.get(month)
        if totals is None:
            continue
        pdf.row([
            (MONTHS[month], 24, Align.L), (str(totals.flights), 20, Align.R),
            (hhmm(totals.block), 22, Align.R), (hhmm(totals.pic), 22, Align.R),
            (hhmm(totals.sic), 22, Align.R), (hhmm(totals.night), 22, Align.R),
            (str(totals.ldg_day + totals.ldg_night), 18, Align.R),
        ])
    pdf.row([
        ("TOTAL", 24, Align.L), (str(grand.flights), 20, Align.R),
        (hhmm(grand.block), 22, Align.R), (hhmm(grand.pic), 22, Align.R),
        (hhmm(grand.sic), 22, Align.R), (hhmm(grand.night), 22, Align.R),
        (str(grand.ldg_day + grand.ldg_night), 18, Align.R),
    ], bold=True)

    return bytes(pdf.output())


def build_month(owner: str, flights: list[Flight], year: int, month: int) -> bytes:
    """Один месяц: каждый рейс строкой."""
    pdf = LogbookPDF(owner, f"{MONTHS[month]} {year}", landscape=True)
    pdf.alias_nb_pages()
    pdf.add_page()
    totals = _detail_pages(pdf, flights)
    _totals_block(pdf, "Month totals", totals)
    return bytes(pdf.output())


def build_full(owner: str, flights: list[Flight]) -> bytes:
    """Вся книжка: каждый рейс строкой, с разбивкой по годам."""
    pdf = LogbookPDF(owner, "Complete logbook", landscape=True)
    pdf.alias_nb_pages()

    grand = Totals()
    by_year: dict[int, list[Flight]] = defaultdict(list)
    for flight in flights:
        by_year[flight.flight_date.year].append(flight)

    # Каждый год с новой страницы: иначе таблица начинается внизу одного
    # листа и продолжается на следующем без заголовка.
    for year in sorted(by_year):
        pdf.section_label = str(year)
        pdf.add_page()
        totals = _detail_pages(pdf, by_year[year])
        for name in Totals.__slots__:
            setattr(grand, name, getattr(grand, name) + getattr(totals, name))

    pdf.section_label = ""
    pdf.add_page()
    _totals_block(pdf, "Grand total", grand)
    return bytes(pdf.output())


def filename(kind: str, period: str | None = None) -> str:
    stamp = date.today().strftime("%Y-%m-%d")
    part = f"-{period}" if period else ""
    return f"logbook-{kind}{part}-{stamp}.pdf"
