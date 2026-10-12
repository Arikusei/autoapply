"""Провайдер Habr Career.

Официальный API Habr Career (`/api/v1/integrations/...`) предназначен для
работодателей и CRM: он умеет читать отклики на *свои* вакансии, но отклика от
имени соискателя в нём нет. Поэтому:

* поиск идёт через открытый `/api/frontend/vacancies` — тот же эндпоинт,
  что использует сайт, без авторизации;
* описание берётся из разметки schema.org JobPosting на странице вакансии;
* отклик возможен только с cookie из твоего браузера и включается явно.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from ..config import Config, SearchSpec
from ..models import ApplyResult, Status, Vacancy
from .base import JobProvider
from .http import BROWSER_USER_AGENT, ApiError, ThrottledClient
from .parsing import extract_json_ld, html_to_text, parse_datetime

BASE_URL = "https://career.habr.com"

# Уровни Habr Career в общей шкале.
QUALIFICATION_TO_SENIORITY = {
    "intern": "intern",
    "junior": "junior",
    "middle": "middle",
    "senior": "senior",
    "lead": "lead",
}

# Числовые id уровней для параметра qid.
QUALIFICATION_IDS = {"intern": 1, "junior": 3, "middle": 4, "senior": 5, "lead": 6}

# Параметры для config.yaml → habr.searches[].params
KNOWN_PARAMS = {
    "q",
    "qid",
    "skills",
    "locations",
    "city_id",
    "remote",
    "employment_type",
    "salary",
    "with_salary",
    "divisions",
    "specializations",
    "company_id",
    "has_accreditation",
    "type",
    "sort",
    "currency",
}

# У Habr Career многозначные фильтры принимаются только в виде name[]=a&name[]=b.
# Если передать name=a&name=b, API молча вернёт результат по одному значению.
BRACKET_PARAMS = {"qid", "skills", "locations", "divisions", "specializations"}

# Из описания JSON-LD Habr начинает описание служебной строкой
# «Навыки: ... Квалификация: ... Специализации: ...» — она полезна для скоринга,
# но её же надо разобрать, чтобы достать уровень.
_QUALIFICATION_RE = re.compile(r"Квалификация:\s*([A-Za-zА-Яа-я]+)")
_SKILLS_RE = re.compile(r"Навыки:\s*([^.]+)\.")


class HabrApiError(ApiError):
    """Ошибка Habr Career."""


class HabrClient(ThrottledClient):
    error_cls = HabrApiError

    def __init__(
        self,
        request_delay: float = 1.0,
        cookies: dict[str, str] | None = None,
        timeout: float = 30.0,
    ):
        super().__init__(
            base_url=BASE_URL,
            user_agent=BROWSER_USER_AGENT,
            request_delay=request_delay,
            timeout=timeout,
            cookies=cookies,
        )

    def search(self, params: list[tuple[str, str]]) -> dict:
        return self.request("GET", "/api/frontend/vacancies", params=params)

    def vacancy_page(self, vacancy_id: str) -> str:
        return self.request("GET", f"/vacancies/{vacancy_id}", raw=True)

    def csrf_token(self) -> str | None:
        try:
            payload = self.request("GET", "/api/frontend_v1/users/authenticity_token")
        except HabrApiError:
            return None
        return (payload or {}).get("token") if isinstance(payload, dict) else None

    def respond(self, vacancy_id: str, letter: str, token: str | None) -> dict:
        headers = {"X-Csrf-Token": token} if token else {}
        result = self.request(
            "POST",
            f"/api/frontend/vacancies/{vacancy_id}/responses",
            json_body={"body": letter},
            headers=headers,
        )
        return result if isinstance(result, dict) else {}


def _flatten_params(params: dict) -> list[tuple[str, str]]:
    """Собрать query-параметры, разворачивая списки в name[]=... ."""
    flat: list[tuple[str, str]] = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            flat.append((key, "true" if value else "false"))
        elif isinstance(value, (list, tuple, set)):
            name = f"{key}[]" if key in BRACKET_PARAMS else key
            flat.extend((name, _stringify(v)) for v in value)
        else:
            flat.append((key, _stringify(value)))
    return flat


def _stringify(value) -> str:
    """Уровни можно писать словами — их удобнее читать в config.yaml."""
    if isinstance(value, str) and value.lower() in QUALIFICATION_IDS:
        return str(QUALIFICATION_IDS[value.lower()])
    return "true" if value is True else "false" if value is False else str(value)


class HabrProvider(JobProvider):
    name = "habr"
    title = "Habr Career"
    known_params = frozenset(KNOWN_PARAMS)

    def __init__(self, config: Config):
        super().__init__(config)
        secrets = config.secrets
        settings = config.settings(self.name)
        self._cookies = {
            k: v
            for k, v in (
                ("remember_user_token", secrets.habr_remember_token),
                ("_career_session", secrets.habr_session),
            )
            if v
        }
        self._request_delay = float(settings.get("request_delay", 1.0))
        # Отклик по cookie — обходной путь, поэтому он выключен по умолчанию.
        self.apply_enabled = bool(settings.get("apply_enabled", False))

    def _build_client(self) -> HabrClient:
        return HabrClient(request_delay=self._request_delay, cookies=self._cookies)

    @property
    def can_apply(self) -> bool:  # type: ignore[override]
        return self.apply_enabled and bool(self._cookies.get("remember_user_token"))

    # --- авторизация ----------------------------------------------------

    def is_authorized(self) -> bool:
        return self.can_apply

    def authorization_hint(self) -> str:
        if not self.apply_enabled:
            return (
                "Автоотклик на Habr Career выключен. Включи его в config.yaml → "
                "habr.apply_enabled: true и добавь cookie в .env "
                "(HABR_REMEMBER_TOKEN, HABR_SESSION) — см. README."
            )
        return (
            "Не заданы cookie Habr Career. Скопируй remember_user_token и "
            "_career_session из браузера в .env (HABR_REMEMBER_TOKEN, HABR_SESSION)."
        )

    # --- поиск ----------------------------------------------------------

    def search(self, spec: SearchSpec) -> Iterator[Vacancy]:
        base = dict(spec.params)
        base.setdefault("sort", "date")
        base.setdefault("type", "all")

        page = 1
        while page <= spec.max_pages:
            params = _flatten_params({**base, "page": page, "per_page": 50})
            payload = self.client.search(params)
            items = (payload or {}).get("list") or []
            for item in items:
                yield self._to_vacancy(item)

            meta = (payload or {}).get("meta") or {}
            total_pages = int(meta.get("totalPages") or 0)
            if page >= total_pages or not items:
                return
            page += 1

    def _to_vacancy(self, item: dict) -> Vacancy:
        salary = item.get("salary") or {}
        company = item.get("company") or {}
        locations = [
            loc.get("title") for loc in (item.get("locations") or []) if loc.get("title")
        ]
        qualification = str(item.get("qualification") or "").strip().lower()
        skills = [s.get("title") for s in (item.get("skills") or []) if s.get("title")]
        published = (item.get("publishedDate") or {}).get("date")
        divisions = [
            d.get("title") for d in (item.get("divisions") or []) if d.get("title")
        ]

        # В списке описания нет — кладём то, что есть, чтобы предфильтр мог
        # отбросить явно чужую вакансию до загрузки страницы.
        description = ", ".join(filter(None, divisions))

        return Vacancy(
            provider=self.name,
            external_id=str(item.get("id")),
            title=item.get("title") or "",
            company=company.get("title") or "",
            url=BASE_URL + (item.get("href") or f"/vacancies/{item.get('id')}"),
            description=description,
            key_skills=skills,
            area_name=", ".join(locations) if locations else None,
            salary_from=salary.get("from"),
            salary_to=salary.get("to"),
            salary_currency=(salary.get("currency") or "").upper() or None,
            experience=item.get("qualification"),
            seniority=QUALIFICATION_TO_SENIORITY.get(qualification),
            employment=item.get("employment"),
            published_at=parse_datetime(published),
            remote=bool(item.get("remoteWork")),
            raw=item,
        )

    def fetch_details(self, vacancy: Vacancy) -> Vacancy:
        """Описание из разметки JobPosting на странице вакансии."""
        try:
            page = self.client.vacancy_page(vacancy.external_id)
        except HabrApiError:
            return vacancy

        posting = extract_json_ld(page, "JobPosting")
        if not posting:
            return vacancy

        description = html_to_text(posting.get("description"))
        if description:
            vacancy.description = description
        if posting.get("title"):
            vacancy.title = posting["title"]
        organization = (posting.get("hiringOrganization") or {}).get("name")
        if organization:
            vacancy.company = organization
        vacancy.published_at = parse_datetime(posting.get("datePosted")) or vacancy.published_at
        vacancy.employment = posting.get("employmentType") or vacancy.employment
        if str(posting.get("jobLocationType") or "").upper() == "TELECOMMUTE":
            vacancy.remote = True

        # Уровень и навыки Habr дублирует в начале описания.
        if not vacancy.seniority:
            found = _QUALIFICATION_RE.search(description)
            if found:
                vacancy.experience = found.group(1)
                vacancy.seniority = QUALIFICATION_TO_SENIORITY.get(
                    found.group(1).strip().lower()
                )
        if not vacancy.key_skills:
            found = _SKILLS_RE.search(description)
            if found:
                vacancy.key_skills = [
                    s.strip() for s in found.group(1).split(",") if s.strip()
                ]

        vacancy.raw = {**vacancy.raw, "jsonld": posting}
        return vacancy

    # --- отклик ---------------------------------------------------------

    def apply(self, vacancy: Vacancy, letter: str) -> ApplyResult:
        if not self.can_apply:
            return ApplyResult(
                status=Status.MANUAL,
                detail=f"нужен ручной отклик — {vacancy.url}",
            )

        try:
            result = self.client.respond(
                vacancy.external_id, letter, self.client.csrf_token()
            )
        except HabrApiError as exc:
            return self._classify_error(exc)

        return ApplyResult(
            status=Status.SENT,
            detail="отклик отправлен",
            negotiation_id=str(result.get("id")) if result.get("id") else None,
        )

    def _classify_error(self, exc: HabrApiError) -> ApplyResult:
        if exc.has("captcha", "капч"):
            return ApplyResult(
                Status.QUEUED,
                "Habr Career требует капчу — отклик из бота заблокирован",
                limit_reached=True,
            )
        if exc.status_code in (401, 403) or exc.has("unauthorized", "авториз"):
            return ApplyResult(
                Status.QUEUED,
                "cookie Habr Career истекли — обнови HABR_* в .env",
                limit_reached=True,
            )
        if exc.has("already", "уже"):
            return ApplyResult(Status.ALREADY, "отклик уже существует")
        if exc.status_code == 404:
            return ApplyResult(Status.UNAVAILABLE, "вакансия снята или скрыта")
        return ApplyResult(Status.FAILED, str(exc))
