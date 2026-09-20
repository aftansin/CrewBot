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

# Названия компаний на латинице. Транслитерация даёт "Pereuchivanie, Ssha",
# что в отчёте для зарубежной авиакомпании выглядит дико.
EMPLOYER_EN = {
    "Аэрофлот": "Aeroflot",
    "Трансаэро": "Transaero",
    "Нордвинд": "Nordwind",
    "Переучивание, США": "Flight training, USA",
    "Переучивание, Сша": "Flight training, USA",
}


def employer_en(name: str | None) -> str:
    if not name:
        return "Unspecified"
    return EMPLOYER_EN.get(name.strip(), latin1(name))

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

    def table_header(
        self, columns: list[tuple[str, int]], aligns: list[str] | None = None
    ) -> None:
        """Заголовок повторяет выравнивание содержимого колонки.

        Иначе подпись стоит по центру, а числа под ней жмутся вправо —
        глаз читает это как сбитую вёрстку.
        """
        self.set_font("Helvetica", "B", 8)
        self.set_fill_color(228, 230, 233)
        for index, (title, width) in enumerate(columns):
            align = aligns[index] if aligns and index < len(aligns) else Align.C
            padding = 1.5 if align != Align.C else 0
            if align == Align.R:
                self.cell(width - padding, 6.5, latin1(title), align=align, fill=True)
                self.cell(padding, 6.5, "", fill=True)
            elif align == Align.L:
                self.cell(padding, 6.5, "", fill=True)
                self.cell(width - padding, 6.5, latin1(title), align=align, fill=True)
            else:
                self.cell(width, 6.5, latin1(title), align=align, fill=True)
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
            padding = 1.5 if align != Align.C else 0
            if align == Align.R:
                self.cell(width - padding, 4.9, latin1(text),
                          border="T" if bold else 0, align=align, fill=fill)
                self.cell(padding, 4.9, "", border="T" if bold else 0, fill=fill)
            elif align == Align.L:
                self.cell(padding, 4.9, "", border="T" if bold else 0, fill=fill)
                self.cell(width - padding, 4.9, latin1(text),
                          border="T" if bold else 0, align=align, fill=fill)
            else:
                self.cell(width, 4.9, latin1(text),
                          border="T" if bold else 0, align=align, fill=fill)
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
# PIC и SIC отдельными колонками не нужны: функция в последнем столбце
# говорит то же самое, а итоги по ним есть в сводном блоке под таблицей.
# Таблица уже полной ширины листа и стоит по центру: растянутая на 277 мм
# она смотрится пустой, а колонки разъезжаются друг от друга.
FLIGHT_COLUMNS = [
    ("DATE", 26), ("FLIGHT", 24), ("FROM", 22), ("TO", 22), ("AIRCRAFT", 32),
    ("OUT", 20), ("IN", 20), ("TOTAL", 24), ("NIGHT", 24), ("FUNCTION", 22),
]
FLIGHT_ALIGNS = [Align.L, Align.L, Align.C, Align.C, Align.L,
                 Align.C, Align.C, Align.R, Align.R, Align.C]
# Отступ слева, чтобы более узкая таблица встала по центру листа.
FLIGHT_INDENT = 14.0


def _flight_row(pdf: LogbookPDF, flight: Flight) -> None:
    tail = flight.aircraft.display if flight.aircraft else ""
    widths = [w for _, w in FLIGHT_COLUMNS]
    values = [
        flight.flight_date.strftime("%d %b %y"),
        latin1(flight.flight_number),
        flight.dep_icao or "",
        flight.arr_icao or "",
        latin1(tail),
        flight.out_utc.strftime("%H:%M") if flight.out_utc else "",
        flight.in_utc.strftime("%H:%M") if flight.in_utc else "",
        hhmm(flight.block_minutes),
        hhmm(flight.night_minutes) if flight.night_minutes else "",
        FUNCTION_SHORT.get(flight.function, ""),
    ]
    pdf.set_x(pdf.l_margin + FLIGHT_INDENT)
    pdf.row([(v, widths[i], FLIGHT_ALIGNS[i]) for i, v in enumerate(values)])


def _flight_table_header(pdf: LogbookPDF) -> None:
    pdf.set_x(pdf.l_margin + FLIGHT_INDENT)
    pdf.table_header(FLIGHT_COLUMNS, FLIGHT_ALIGNS)


def _detail_pages(pdf: LogbookPDF, flights: list[Flight]) -> Totals:
    grand = Totals()
    _flight_table_header(pdf)
    for flight in sorted(flights, key=lambda f: (f.flight_date, f.out_utc or datetime.min)):
        if pdf.get_y() > pdf.h - 26:
            pdf.add_page()
            _flight_table_header(pdf)
        _flight_row(pdf, flight)
        grand.add(flight)

    widths = [w for _, w in FLIGHT_COLUMNS]
    pdf.set_x(pdf.l_margin + FLIGHT_INDENT)
    pdf.row(
        [
            ("TOTAL", sum(widths[:7]), Align.R),
            (hhmm(grand.block), widths[7], Align.R),
            (hhmm(grand.night), widths[8], Align.R),
            ("", widths[9], Align.C),
        ],
        bold=True,
    )
    return grand


# Одинаковые ширины во всех таблицах сводки: разнобой колонок на одной
# странице читается как небрежность.
SUMMARY_COLUMNS = [("", 74), ("FLIGHTS", 20), ("TOTAL", 24), ("PIC", 24),
                   ("SIC", 24), ("NIGHT", 24)]


def _summary_table(pdf: LogbookPDF, first_header: str, rows: list[tuple[str, Totals]],
                   total: Totals | None = None) -> None:
    columns = [(first_header, SUMMARY_COLUMNS[0][1])] + SUMMARY_COLUMNS[1:]
    pdf.table_header(columns, [Align.L] + [Align.R] * (len(columns) - 1))
    widths = [w for _, w in columns]
    for label, totals in rows:
        pdf.row([
            (label[:46], widths[0], Align.L),
            (str(totals.flights), widths[1], Align.R),
            (hhmm(totals.block), widths[2], Align.R),
            (hhmm(totals.pic), widths[3], Align.R),
            (hhmm(totals.sic), widths[4], Align.R),
            (hhmm(totals.night), widths[5], Align.R),
        ])
    if total is not None:
        pdf.row([
            ("TOTAL", widths[0], Align.L),
            (str(total.flights), widths[1], Align.R),
            (hhmm(total.block), widths[2], Align.R),
            (hhmm(total.pic), widths[3], Align.R),
            (hhmm(total.sic), widths[4], Align.R),
            (hhmm(total.night), widths[5], Align.R),
        ], bold=True)


def build_summary(owner: str, flights: list[Flight]) -> bytes:
    """Одна страница: итоги по годам, компаниям и типам ВС. Для резюме."""
    pdf = LogbookPDF(owner, "Summary of flight experience")
    pdf.alias_nb_pages()
    pdf.add_page()

    by_year: dict[int, Totals] = defaultdict(Totals)
    by_type: dict[str, Totals] = defaultdict(Totals)
    by_employer: dict[str, Totals] = defaultdict(Totals)
    employer_span: dict[str, list[date]] = defaultdict(list)
    grand = Totals()

    for flight in flights:
        by_year[flight.flight_date.year].add(flight)
        grand.add(flight)
        kind = flight.aircraft.type_name if flight.aircraft else None
        by_type[latin1(kind) or "Unspecified"].add(flight)

        employer = flight.employer
        name = employer_en(employer.name) if employer else "Unspecified"
        # Один работодатель может встречаться дважды — уход и возвращение.
        # Разделяем такие периоды по дате начала.
        key = f"{name}|{employer.started_on:%Y}" if employer else name
        by_employer[key].add(flight)
        employer_span[key].append(flight.flight_date)

    _totals_block(pdf, "Grand total", grand)

    pdf.section("By employer")
    employer_rows = []
    for key in sorted(by_employer, key=lambda k: min(employer_span[k])):
        name = key.split("|")[0]
        dates = employer_span[key]
        span = f"{min(dates):%m.%Y} - {max(dates):%m.%Y}"
        employer_rows.append((f"{name}  ({span})", by_employer[key]))
    _summary_table(pdf, "EMPLOYER", employer_rows)

    pdf.section("By year")
    _summary_table(
        pdf, "YEAR", [(str(y), by_year[y]) for y in sorted(by_year)], grand
    )

    pdf.section("By aircraft type")
    _summary_table(
        pdf,
        "TYPE",
        [(k, by_type[k]) for k in sorted(by_type, key=lambda k: -by_type[k].block)],
    )

    return bytes(pdf.output())


# Книжный лист: полезная ширина 190 мм.
YEAR_COLUMNS = [
    ("MONTH", 28), ("FLIGHTS", 22), ("TOTAL", 26), ("PIC", 26),
    ("SIC", 26), ("NIGHT", 26), ("DAY LDG", 18), ("NIGHT LDG", 18),
]
YEAR_ALIGNS = [Align.L] + [Align.R] * 7


def build_year(owner: str, flights: list[Flight], year: int) -> bytes:
    """Один год: месяцы таблицей плюс итог. Альбомный лист, широкая таблица."""
    pdf = LogbookPDF(owner, f"Year {year}")
    pdf.alias_nb_pages()
    pdf.add_page()

    by_month: dict[int, Totals] = defaultdict(Totals)
    by_type: dict[str, Totals] = defaultdict(Totals)
    by_route: dict[str, Totals] = defaultdict(Totals)
    grand = Totals()
    for flight in flights:
        by_month[flight.flight_date.month].add(flight)
        grand.add(flight)
        kind = flight.aircraft.type_name if flight.aircraft else None
        by_type[latin1(kind) or "Unspecified"].add(flight)
        if flight.dep_icao and flight.arr_icao:
            by_route[f"{flight.dep_icao} - {flight.arr_icao}"].add(flight)

    pdf.table_header(YEAR_COLUMNS, YEAR_ALIGNS)
    widths = [w for _, w in YEAR_COLUMNS]
    for month in range(1, 13):
        totals = by_month.get(month)
        values = (
            [str(totals.flights), hhmm(totals.block), hhmm(totals.pic),
             hhmm(totals.sic), hhmm(totals.night), str(totals.ldg_day),
             str(totals.ldg_night)]
            if totals
            # Пустые месяцы показываем прочерками: пропуск в таблице
            # читается как потерянные данные.
            else ["-"] * 7
        )
        pdf.row(
            [(MONTHS[month], widths[0], Align.L)]
            + [(v, widths[i + 1], Align.R) for i, v in enumerate(values)]
        )

    pdf.row(
        [("TOTAL", widths[0], Align.L)]
        + [
            (v, widths[i + 1], Align.R)
            for i, v in enumerate([
                str(grand.flights), hhmm(grand.block), hhmm(grand.pic),
                hhmm(grand.sic), hhmm(grand.night), str(grand.ldg_day),
                str(grand.ldg_night),
            ])
        ],
        bold=True,
    )

    # Год в книжной ориентации оставляет полстраницы пустыми — заполняем
    # тем, что реально интересно посмотреть за год.
    pdf.section("By aircraft type")
    _summary_table(
        pdf, "TYPE",
        [(k, by_type[k]) for k in sorted(by_type, key=lambda k: -by_type[k].block)],
    )

    top_routes = sorted(by_route, key=lambda k: -by_route[k].flights)[:12]
    if top_routes:
        pdf.section("Most flown routes")
        _summary_table(pdf, "ROUTE", [(k, by_route[k]) for k in top_routes])

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
