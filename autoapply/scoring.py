"""Скоринг вакансий по профилю кандидата.

Логика в два шага: сначала жёсткие фильтры (стоп-слова, уровень), потом
накопительный балл по ключевым словам и условиям. Каждое решение сопровождается
причиной — её видно в `review`, по ней настраивается config.

Скоринг одинаков для всех площадок: провайдеры приводят уровень вакансии к общей
шкале SENIORITY_ORDER, поэтому правила отбора не зависят от того, откуда вакансия.
"""

from __future__ import annotations

from .config import ScoringSpec
from .matching import find_substrings, find_words
from .models import SENIORITY_LABELS, Score, Vacancy


def _area_matches(vacancy: Vacancy, home_names: list[str]) -> bool:
    """Вакансия в домашнем регионе.

    Сравнение по названию, а не по id: у каждой площадки свои идентификаторы
    регионов, а «Томск» пишется одинаково.
    """
    if not home_names or not vacancy.area_name:
        return False
    area = vacancy.area_name.lower()
    return any(name in area for name in home_names)


def hard_filter(vacancy: Vacancy, spec: ScoringSpec) -> str | None:
    """Причина безусловного отказа или None.

    Вызывается дважды: по краткому описанию из поиска (чтобы не тратить запрос
    и токены на явно чужую вакансию) и повторно по полному описанию.
    """
    text = vacancy.searchable_text()

    blocked = find_words(text, spec.blocklist)
    if blocked:
        return f"стоп-слово в описании: {', '.join(blocked[:5])}"

    title_hits = find_words(vacancy.title.lower(), spec.reject_title_patterns)
    if title_hits:
        return f"уровень не подходит, в названии: {', '.join(title_hits)}"

    if (
        spec.allow_seniority
        and vacancy.seniority
        and vacancy.seniority not in spec.allow_seniority
    ):
        label = SENIORITY_LABELS.get(vacancy.seniority, vacancy.seniority)
        return f"требуемый уровень: {label}"

    if vacancy.has_test:
        return "вакансия с тестом — отклик через API недоступен"

    return None


def score_vacancy(vacancy: Vacancy, spec: ScoringSpec) -> Score:
    text = vacancy.searchable_text()
    reasons: list[str] = []

    rejection = hard_filter(vacancy, spec)
    if rejection:
        return Score(value=0, accepted=False, reasons=[rejection])

    matched = find_substrings(text, list(spec.weights))
    value = sum(spec.weights[kw] for kw in matched)
    if matched:
        top = sorted(matched, key=lambda kw: -spec.weights[kw])[:8]
        reasons.append("совпадения: " + ", ".join(top))

    if spec.require_any and not find_substrings(text, spec.require_any):
        return Score(
            value=value,
            accepted=False,
            reasons=["нет ни одного ключевого навыка из профиля"],
            matched=matched,
        )

    bonuses = spec.bonuses or {}
    if vacancy.remote and bonuses.get("remote"):
        value += bonuses["remote"]
        reasons.append(f"удалённая работа (+{bonuses['remote']})")
    if (vacancy.salary_from or vacancy.salary_to) and bonuses.get("salary_specified"):
        value += bonuses["salary_specified"]
        reasons.append(f"указана зарплата (+{bonuses['salary_specified']})")
    if bonuses.get("area_match") and _area_matches(vacancy, spec.home_area_names):
        value += bonuses["area_match"]
        reasons.append(f"домашний регион (+{bonuses['area_match']})")

    accepted = value >= spec.threshold
    if not accepted:
        reasons.append(f"балл {value} ниже порога {spec.threshold}")

    return Score(value=value, accepted=accepted, reasons=reasons, matched=matched)
