"""Сопоставление имён, записанных разными алфавитами.

Лента расписания отдаёт ФИО кириллицей ("Евстифеев Андрей Валерьевич"),
а книжка, перенесённая из LogTen, хранит латиницу ("Evstifeev Andrey").
Это один человек, но буквального совпадения нет, и без приведения к
общему виду бот заводит на него вторую карточку.

Латинские написания тоже расходятся между собой: "Kodryan" и "Kodrian",
"Dzivalkovskiy" и "Dzivalkovskii". Поэтому мало транслитерировать —
нужно ещё свести к одному виду те сочетания, которые в разных системах
передаются по-разному.

Ключ намеренно огрубляющий, но не настолько, чтобы слить разных людей:
"Evstifeev" и "Evstafev" остаются разными, хотя отличаются двумя буквами.
"""

from __future__ import annotations

import re

# Кириллица -> латиница. Многобуквенные сочетания идут первыми,
# иначе "щ" разберётся как "ш" + "ч".
CYRILLIC_TO_LATIN = {
    "щ": "shch", "ё": "e", "ж": "zh", "ц": "ts", "ч": "ch", "ш": "sh",
    # "я" и "ю" передают как ya/ia и yu/iu — берём вариант с "i",
    # к нему же сводится и вариант с "y" ниже.
    "ю": "iu", "я": "ia", "х": "kh", "ъ": "", "ь": "", "й": "y", "ы": "y",
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "з": "z",
    "и": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p",
    "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "э": "e",
}

# Сочетания, которые в разных системах транслитерации передаются по-разному.
# Сводим их к одной букве. Порядок важен: длинные раньше коротких.
COLLAPSE = (
    ("shch", "s"), ("sch", "s"), ("sh", "s"), ("ch", "c"), ("ts", "c"),
    ("zh", "z"), ("kh", "h"),
    # Сначала двухбуквенные с "y", иначе общее правило y -> i разорвёт их.
    ("yu", "iu"), ("ya", "ia"), ("ie", "e"),
    ("j", "i"), ("y", "i"), ("w", "v"), ("q", "k"), ("x", "ks"),
)

_NON_LETTER = re.compile(r"[^a-z]")


def to_latin(text: str) -> str:
    """Переводит кириллицу в латиницу, латиницу оставляет как есть."""
    result = []
    for char in (text or "").lower():
        result.append(CYRILLIC_TO_LATIN.get(char, char))
    return "".join(result)


def name_key(text: str) -> str:
    """Огрублённый ключ имени: два написания одного человека дают один ключ.

    "Кодрян", "Kodryan" и "Kodrian" -> один ключ
    "Евстифеев" и "Evstifeev" -> "evstifeev"
    "Евстафьев" -> "evstafev" — остаётся отличным, это другой человек.
    """
    value = to_latin(text)
    value = _NON_LETTER.sub("", value)
    for source, target in COLLAPSE:
        value = value.replace(source, target)
    # Двойные буквы в разных системах то удваиваются, то нет.
    collapsed = []
    for char in value:
        if not collapsed or collapsed[-1] != char:
            collapsed.append(char)
    return "".join(collapsed)


def person_key(last: str | None, first: str | None = None) -> str:
    """Ключ по фамилии и имени. Отчество не участвует: в книжке его часто нет."""
    parts = [name_key(last or "")]
    if first:
        parts.append(name_key(first))
    return "|".join(p for p in parts if p)


def same_person(
    last_a: str | None, first_a: str | None, last_b: str | None, first_b: str | None
) -> bool:
    """Совпадают ли написания. Требует и фамилию, и имя.

    Только по фамилии не сравниваем: однофамильцы в экипажах встречаются,
    и слить их было бы хуже, чем не слить вовсе.
    """
    if not last_a or not last_b:
        return False
    if name_key(last_a) != name_key(last_b):
        return False
    if not first_a or not first_b:
        # Фамилия совпала, но одно из имён неизвестно — не рискуем.
        return False
    return name_key(first_a) == name_key(first_b)


_CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def is_cyrillic(text: str | None) -> bool:
    return bool(_CYRILLIC.search(text or ""))


def same_script(a: str | None, b: str | None) -> bool:
    """Одним ли алфавитом записаны обе строки.

    Нужно, чтобы при склейке карточек не получалось
    "Evstifeev Andrey Валерьевич".
    """
    return is_cyrillic(a) == is_cyrillic(b)


# Написание для показа и отчётов. В отличие от name_key, здесь ничего
# не огрубляется: "Тарабрина" -> "Tarabrina", а не "tarabrina".
DISPLAY_TRANSLIT = {
    "щ": "shch", "ё": "e", "ж": "zh", "ц": "ts", "ч": "ch", "ш": "sh",
    "ю": "yu", "я": "ya", "х": "kh", "ъ": "", "ь": "", "й": "y", "ы": "y",
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "з": "z",
    "и": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p",
    "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "э": "e",
}


def to_display_latin(text: str | None) -> str | None:
    """Кириллицу переводит в латиницу для показа, латиницу не трогает.

    Нужно, потому что лента расписания отдаёт ФИО кириллицей, а отчёты
    уходят в зарубежные авиакомпании — там кириллицу не прочитают.
    Написание из ленты при этом не теряется: оно сохраняется в алиасах,
    и поиск работает по обоим вариантам.

    Дефисные фамилии и составные имена сохраняют разделители:
    "Иванов-Петров" -> "Ivanov-Petrov".
    """
    if not text:
        return text
    if not is_cyrillic(text):
        return text

    result = []
    for part in re.split(r"([ \-'])", text):
        if not part or part in " -'":
            result.append(part)
            continue
        converted = "".join(DISPLAY_TRANSLIT.get(c, c) for c in part.lower())
        result.append(converted[:1].upper() + converted[1:] if converted else "")
    return "".join(result)
