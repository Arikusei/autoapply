"""Разбор HTML и дат — общий для всех площадок."""

from __future__ import annotations

import html
import json
import re
from datetime import datetime

# Блочные теги превращаем в переносы строк, чтобы списки требований
# не склеивались в одну строку и оставались читаемыми в `review`.
_BLOCK_BREAK = re.compile(r"</(p|div|li|ul|ol|h[1-6]|tr|table)>|<br\s*/?>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_BLANK_LINES = re.compile(r"\n{3,}")
_JSON_LD = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I
)


def html_to_text(raw: str | None) -> str:
    """HTML-описание вакансии в читаемый плоский текст."""
    if not raw:
        return ""
    text = _BLOCK_BREAK.sub("\n", raw)
    text = _TAG.sub("", text)
    text = html.unescape(text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANK_LINES.sub("\n\n", text).strip()


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def extract_json_ld(page: str, expected_type: str) -> dict | None:
    """Найти на странице блок JSON-LD нужного типа.

    Habr Career не отдаёт JSON с описанием вакансии, но публикует на странице
    разметку schema.org JobPosting — это открытый стандарт, рассчитанный на
    машинное чтение, и брать описание оттуда надёжнее, чем парсить вёрстку.
    """
    for block in _JSON_LD.findall(page):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if isinstance(item, dict) and item.get("@type") == expected_type:
                return item
    return None
