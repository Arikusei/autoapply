"""Провайдеро-независимые модели данных."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

# Единая шкала уровней. Каждая площадка называет уровни по-своему
# (hh.ru — через требуемый опыт, Habr — Intern/Junior/..., getmatch — junior/...),
# поэтому провайдеры приводят своё значение к этой шкале, а скоринг работает
# с одним понятным списком разрешённых уровней.
SENIORITY_ORDER = ("intern", "junior", "middle", "senior", "lead")

SENIORITY_LABELS = {
    "intern": "стажёр",
    "junior": "junior",
    "middle": "middle",
    "senior": "senior",
    "lead": "lead",
}


def lowest_seniority(levels: list[str]) -> str | None:
    """Самый младший уровень из списка.

    Вакансия, открытая для junior и senior одновременно, считается доступной
    junior-кандидату — поэтому берётся минимум, а не первый элемент.
    """
    known = [lvl for lvl in levels if lvl in SENIORITY_ORDER]
    if not known:
        return None
    return min(known, key=SENIORITY_ORDER.index)


class Status(str, Enum):
    """Состояние вакансии в очереди."""

    QUEUED = "queued"            # прошла скоринг, письмо готово, ждёт отправки
    SKIPPED = "skipped"          # не прошла скоринг
    SENT = "sent"                # отклик отправлен
    FAILED = "failed"            # отправка сорвалась, можно повторить
    ALREADY = "already"          # отклик уже был
    UNAVAILABLE = "unavailable"  # архив/тест/капча — повторять бессмысленно
    MANUAL = "manual"            # площадка не даёт откликаться из бота


@dataclass
class Vacancy:
    """Вакансия, приведённая к общему виду."""

    provider: str
    external_id: str
    title: str
    company: str
    url: str
    description: str = ""
    key_skills: list[str] = field(default_factory=list)
    area_id: str | None = None
    area_name: str | None = None
    salary_from: int | None = None
    salary_to: int | None = None
    salary_currency: str | None = None
    # Требуемый опыт в терминах площадки, как пришёл из API.
    experience: str | None = None
    # Тот же уровень, приведённый к SENIORITY_ORDER.
    seniority: str | None = None
    schedule: str | None = None
    employment: str | None = None
    published_at: datetime | None = None
    remote: bool = False
    has_test: bool = False
    letter_required: bool = False
    raw: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.external_id}"

    @property
    def salary_text(self) -> str:
        if self.salary_from is None and self.salary_to is None:
            return "—"
        cur = self.salary_currency or ""
        if self.salary_from and self.salary_to:
            return f"{self.salary_from:,}–{self.salary_to:,} {cur}".replace(",", " ")
        if self.salary_from:
            return f"от {self.salary_from:,} {cur}".replace(",", " ")
        return f"до {self.salary_to:,} {cur}".replace(",", " ")

    def searchable_text(self) -> str:
        """Всё текстовое содержимое вакансии одной строкой — для скоринга."""
        parts = [self.title, self.company, self.description, " ".join(self.key_skills)]
        return "\n".join(p for p in parts if p).lower()


@dataclass
class Score:
    """Результат скоринга: балл, вердикт и объяснение."""

    value: int
    accepted: bool
    reasons: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {"value": self.value, "reasons": self.reasons, "matched": self.matched},
            ensure_ascii=False,
        )


@dataclass
class ApplyResult:
    """Итог попытки отклика."""

    status: Status
    detail: str = ""
    negotiation_id: str | None = None
    # Провайдер сообщил, что лимит исчерпан — прогон надо остановить.
    limit_reached: bool = False
