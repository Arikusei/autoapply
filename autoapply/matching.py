"""Поиск ключевых слов в тексте вакансии.

Здесь две разные задачи, и они требуют разных правил сравнения:

* Стоп-слова и уровни в названии сравниваются **по границам слова**. Иначе
  «Java» не находится в «FullStack QA (Java)», зато «Java» ошибочно находится
  в «JavaScript». Границы слова решают оба случая сразу.
* Веса и require_any сравниваются **по подстроке**, потому что там намеренно
  указаны основы слов: «автоматизац» должно находиться и в «автоматизация»,
  и в «автоматизации».
"""

from __future__ import annotations

import re
from functools import lru_cache

_WORD_EDGE = re.compile(r"\w", re.UNICODE)


def _to_pattern(term: str) -> re.Pattern[str]:
    """Регулярка на точное слово, устойчивая к символам вроде «c++» и «1с:».

    Граница слова добавляется только с той стороны, где терм начинается или
    заканчивается буквой или цифрой: для «c++» справа границы быть не может,
    иначе выражение никогда не сработает.
    """
    pattern = re.escape(term)
    if term and _WORD_EDGE.match(term[0]):
        pattern = r"\b" + pattern
    if term and _WORD_EDGE.match(term[-1]):
        pattern = pattern + r"\b"
    return re.compile(pattern, re.IGNORECASE | re.UNICODE)


@lru_cache(maxsize=64)
def _compile_all(terms: tuple[str, ...]) -> tuple[tuple[str, re.Pattern[str]], ...]:
    return tuple((term, _to_pattern(term)) for term in terms)


def find_words(text: str, terms: list[str]) -> list[str]:
    """Термы, найденные в тексте как отдельные слова."""
    if not terms or not text:
        return []
    return [term for term, pattern in _compile_all(tuple(terms)) if pattern.search(text)]


def find_substrings(text: str, terms: list[str]) -> list[str]:
    """Термы, найденные в тексте как подстроки (основы слов)."""
    if not terms or not text:
        return []
    return [term for term in terms if term in text]
