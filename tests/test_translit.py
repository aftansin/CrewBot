"""Тесты сопоставления имён между алфавитами.

Лента отдаёт ФИО кириллицей, книжка из LogTen — латиницей. Без сведения
к общему виду бот заводит вторую карточку на того же человека.
"""
from __future__ import annotations

from app.logbook.translit import is_cyrillic, name_key, person_key, same_person, same_script

# Пары, взятые из реального справочника: слева лента, справа книжка.
SAME = [
    ("Евстифеев", "Андрей", "Evstifeev", "Andrey"),
    ("Кодрян", "Валентин", "Kodryan", "Valentin"),
    ("Кодрян", "Валентин", "Kodrian", "Valentin"),
    ("Тарабрина", "Ольга", "Tarabrina", "Olga"),
    ("Егеубаев", "Руслан", "Egeubaev", "Ruslan"),
    ("Бортнев", "Егор", "Bortnev", "Egor"),
    ("Дзивалковский", "Иван", "Dzivalkovskiy", "Ivan"),
    ("Дзивалковский", "Иван", "Dzivalkovskii", "Ivan"),
    ("Шагиахметов", "Фаиль", "Shagiakhmetov", "Fail"),
    ("Лохматиков", "Никита", "Lokhmatikov", "Nikita"),
    ("Долгополов", "Сергей", "Dolgopolov", "Sergei"),
    ("Долгополов", "Сергей", "Dolgopolov", "Sergey"),
]

DIFFERENT = [
    # Отличаются двумя буквами, но это разные люди из того же справочника.
    ("Евстифеев", "Андрей", "Evstafev", "Andrey"),
    ("Кодрян", "Валентин", "Кондрян", "Валентин"),
    ("Иванов", "Пётр", "Иванов", "Павел"),
    ("Петров", "Иван", "Петухов", "Иван"),
]


def test_cross_alphabet_spellings_match():
    for last_a, first_a, last_b, first_b in SAME:
        assert same_person(last_a, first_a, last_b, first_b), f"{last_a} != {last_b}"


def test_distinct_people_are_not_merged():
    for last_a, first_a, last_b, first_b in DIFFERENT:
        assert not same_person(last_a, first_a, last_b, first_b), f"{last_a} == {last_b}"


def test_surname_alone_is_not_enough():
    """Однофамильцы в экипажах встречаются: слить их хуже, чем не слить."""
    assert not same_person("Иванов", None, "Ivanov", "Petr")
    assert not same_person("Иванов", "Петр", "Ivanov", None)


def test_transliteration_variants_of_ya_and_yu():
    """"я" и "ю" передают как ya/ia и yu/iu — оба варианта дают один ключ."""
    assert name_key("Кодрян") == name_key("Kodryan") == name_key("Kodrian")
    assert name_key("Юрьев") == name_key("Yuriev") == name_key("Iuriev")


def test_person_key_ignores_patronymic():
    """В книжке отчества часто нет, в ленте оно есть — на сравнение не влияет."""
    assert person_key("Евстифеев", "Андрей") == person_key("Evstifeev", "Andrey")


def test_script_detection():
    assert is_cyrillic("Валерьевич")
    assert not is_cyrillic("Valeryevich")
    # Защита от "Evstifeev Andrey Валерьевич" при склейке карточек.
    assert not same_script("Evstifeev", "Валерьевич")
    assert same_script("Евстифеев", "Валерьевич")


def test_empty_values_are_safe():
    assert name_key("") == ""
    assert not same_person("", "", "", "")
    assert not same_person(None, None, "Ivanov", "Ivan")


def test_display_transliteration():
    """Отчёты уходят за рубеж — кириллицу там не прочитают."""
    from app.logbook.translit import to_display_latin

    assert to_display_latin("Тарабрина") == "Tarabrina"
    assert to_display_latin("Дзивалковский") == "Dzivalkovskiy"
    assert to_display_latin("Щербаков") == "Shcherbakov"
    assert to_display_latin("Чернышёв") == "Chernyshev"
    assert to_display_latin("Иванов-Петров") == "Ivanov-Petrov"


def test_display_leaves_latin_untouched():
    from app.logbook.translit import to_display_latin

    assert to_display_latin("Evstifeev") == "Evstifeev"
    assert to_display_latin(None) is None
    assert to_display_latin("") == ""


def test_transliterated_name_still_matches_original():
    """Перевод в латиницу не должен рвать связь с исходным написанием."""
    from app.logbook.translit import to_display_latin

    for last, first in (("Тарабрина", "Ольга"), ("Егеубаев", "Руслан")):
        assert same_person(last, first, to_display_latin(last), to_display_latin(first))
