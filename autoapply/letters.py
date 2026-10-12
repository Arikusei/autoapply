"""Генерация сопроводительных писем.

Письмо создаётся на этапе `scan` и складывается в очередь вместе с вакансией.
Поэтому в `review` виден именно тот текст, который уйдёт работодателю.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from .config import Config, LettersSpec, Secrets
from .models import Vacancy

# Больше в промпт не влезает ничего полезного, а токены экономит заметно.
MAX_DESCRIPTION_CHARS = 3500

SYSTEM_PROMPT = """Ты помогаешь соискателю писать сопроводительные письма к вакансиям на hh.ru.

Требования к письму:
- Русский язык, деловой но живой тон, без канцелярита и пафоса.
- Объём: 900–1400 символов. Три-четыре абзаца.
- Начни с обращения «Здравствуйте!» и назови вакансию.
- Свяжи 2–3 конкретных требования вакансии с реальным опытом кандидата из его профиля.
- Заверши короткой фразой о готовности обсудить задачи и подписью с контактами.

Категорически запрещено:
- Придумывать опыт, технологии, места работы или цифры, которых нет в профиле.
- Заявлять навыки из раздела never_claim.
- Преувеличивать стаж: у кандидата его немного, это нормально, делай упор на релевантность и скорость обучения.
- Markdown, списки, заголовки, эмодзи. Только связный текст с переносами строк.

Верни только текст письма, без пояснений и кавычек вокруг него."""


class LetterError(RuntimeError):
    """LLM недоступна или вернула непригодный ответ."""


def _profile_summary(profile: dict[str, Any]) -> str:
    """Профиль кандидата в компактный текст для промпта."""
    lines: list[str] = []
    if profile.get("name"):
        lines.append(f"Имя: {profile['name']}")
    if profile.get("location"):
        lines.append(f"Город: {profile['location']}")
    if profile.get("headline"):
        lines.append(f"Позиционирование: {profile['headline'].strip()}")

    for job in profile.get("experience") or []:
        lines.append(
            f"\nОпыт: {job.get('role', '')} — {job.get('company', '')} ({job.get('period', '')})"
        )
        lines.extend(f"  - {h}" for h in (job.get("highlights") or []))

    skills = profile.get("skills") or {}
    if skills:
        lines.append("\nНавыки:")
        for group, items in skills.items():
            if items:
                lines.append(f"  {group}: {', '.join(str(i) for i in items)}")

    if profile.get("education"):
        lines.append("\nОбразование:")
        lines.extend(f"  - {e}" for e in profile["education"])

    if profile.get("achievements"):
        lines.append("\nДостижения:")
        lines.extend(f"  - {a}" for a in profile["achievements"])

    constraints = profile.get("constraints") or {}
    if constraints:
        lines.append("\nОграничения (соблюдать строго):")
        if constraints.get("total_years_experience"):
            lines.append(f"  - суммарный опыт ~{constraints['total_years_experience']} года")
        if constraints.get("seniority"):
            lines.append(f"  - уровень: {constraints['seniority']}")
        for item in constraints.get("never_claim") or []:
            lines.append(f"  - never_claim: {item}")

    if profile.get("email") or profile.get("phone"):
        contacts = ", ".join(
            str(profile[k]) for k in ("phone", "email", "github") if profile.get(k)
        )
        lines.append(f"\nКонтакты для подписи: {contacts}")

    return "\n".join(lines)


def _vacancy_brief(vacancy: Vacancy) -> str:
    description = vacancy.description[:MAX_DESCRIPTION_CHARS]
    if len(vacancy.description) > MAX_DESCRIPTION_CHARS:
        description += "\n[описание обрезано]"
    parts = [
        f"Вакансия: {vacancy.title}",
        f"Компания: {vacancy.company}",
        f"Регион: {vacancy.area_name or '—'}",
        f"Зарплата: {vacancy.salary_text}",
    ]
    if vacancy.key_skills:
        parts.append(f"Ключевые навыки: {', '.join(vacancy.key_skills)}")
    parts.append(f"\nОписание:\n{description}")
    return "\n".join(parts)


_MARKDOWN = re.compile(r"[*_`#]+")


def _clean(text: str, max_chars: int) -> str:
    """Убрать markdown и аккуратно обрезать по границе абзаца/предложения."""
    text = _MARKDOWN.sub("", text).strip().strip('"').strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) <= max_chars:
        return text

    cut = text[:max_chars]
    for boundary in ("\n\n", ". ", ".\n"):
        idx = cut.rfind(boundary)
        if idx > max_chars * 0.6:
            return cut[: idx + len(boundary)].strip()
    return cut.rstrip() + "…"


def _call_openai(spec: LettersSpec, secrets: Secrets, system: str, user: str) -> str:
    if not secrets.openai_api_key:
        raise LetterError("Не задан OPENAI_API_KEY в .env")
    response = httpx.post(
        f"{secrets.openai_base_url.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {secrets.openai_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": spec.model,
            "temperature": spec.temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        },
        timeout=90.0,
    )
    if response.status_code >= 400:
        raise LetterError(f"OpenAI HTTP {response.status_code}: {response.text[:300]}")
    try:
        return response.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as exc:
        raise LetterError(f"Неожиданный ответ OpenAI: {exc}") from exc


def _call_anthropic(spec: LettersSpec, secrets: Secrets, system: str, user: str) -> str:
    if not secrets.anthropic_api_key:
        raise LetterError("Не задан ANTHROPIC_API_KEY в .env")
    response = httpx.post(
        f"{secrets.anthropic_base_url.rstrip('/')}/messages",
        headers={
            "x-api-key": secrets.anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": spec.model,
            "max_tokens": 1200,
            "temperature": spec.temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=90.0,
    )
    if response.status_code >= 400:
        raise LetterError(f"Anthropic HTTP {response.status_code}: {response.text[:300]}")
    try:
        return response.json()["content"][0]["text"]
    except (KeyError, IndexError, ValueError) as exc:
        raise LetterError(f"Неожиданный ответ Anthropic: {exc}") from exc


class LetterWriter:
    """Отдаёт письмо и источник: 'llm' или 'template'."""

    def __init__(self, config: Config):
        self.spec = config.letters
        self.secrets = config.secrets
        self.profile_text = _profile_summary(config.profile)
        # Если LLM однажды отказала (нет ключа, кончились деньги), не долбим её
        # на каждой вакансии — молча уходим в шаблон.
        self.llm_disabled_reason: str | None = None

    def render_template(self, vacancy: Vacancy) -> str:
        template = self.spec.template or (
            "Здравствуйте! Меня заинтересовала вакансия «{title}» в компании {company}."
        )
        try:
            text = template.format(
                title=vacancy.title.strip(),
                # Названия вроде «EX CORP.» иначе дают двойную точку в письме.
                company=vacancy.company.strip().rstrip("."),
                area=vacancy.area_name or "",
            )
        except KeyError as exc:
            raise LetterError(
                f"В шаблоне письма используется неизвестное поле {exc}."
            ) from exc
        return _clean(text, self.spec.max_chars)

    def write(self, vacancy: Vacancy) -> tuple[str, str]:
        if self.spec.mode != "llm":
            return self.render_template(vacancy), "template"

        if self.llm_disabled_reason is None:
            user_prompt = (
                f"ПРОФИЛЬ КАНДИДАТА\n{self.profile_text}\n\n"
                f"ВАКАНСИЯ\n{_vacancy_brief(vacancy)}\n\n"
                "Напиши сопроводительное письмо."
            )
            try:
                if self.spec.llm_provider == "anthropic":
                    raw = _call_anthropic(
                        self.spec, self.secrets, SYSTEM_PROMPT, user_prompt
                    )
                else:
                    raw = _call_openai(
                        self.spec, self.secrets, SYSTEM_PROMPT, user_prompt
                    )
                letter = _clean(raw, self.spec.max_chars)
                if len(letter) < 200:
                    raise LetterError(f"письмо подозрительно короткое ({len(letter)} симв.)")
                return letter, "llm"
            except (LetterError, httpx.HTTPError) as exc:
                if not self.spec.fallback_to_template:
                    raise
                self.llm_disabled_reason = str(exc)

        return self.render_template(vacancy), "template"
