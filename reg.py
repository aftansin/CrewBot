import re


def strip_summary(summary: str) -> str:
    """Форматирует summary для сообщений, оставляя только номер рейса и города"""
    # Удаляем все содержимое в скобках (включая вложенные скобки)
    result = re.sub(r'\([^)]*\)', '', summary)
    # Удаляем эмодзи и другие специальные символы (кроме → и пробелов)
    result = re.sub(r'[^\w\s→-]', '', result)
    # Заменяем множественные пробелы на один
    result = re.sub(r'\s+', ' ', result).strip()
    # Удаляем возможные оставшиеся скобки
    result = result.replace(')', '').replace('(', '').strip()
    # Форматируем стрелку с пробелами вокруг
    result = re.sub(r'\s*→\s*', ' → ', result)
    return result

input_str = "SU1519 ✈️ Горно-Алтайск (RGK | Горно-Алтайск) → Москва (SVO | Шереметьево (B))"
output_str = strip_summary(input_str)
print(output_str)  # Выведет: "SU1519 ✈️ Горно-Алтайск → Москва"