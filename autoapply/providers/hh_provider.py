"""Провайдер hh.ru — единственная площадка с официальным API для отклика."""

from __future__ import annotations

from collections.abc import Iterator

from ..config import Config, SearchSpec, TOKENS_PATH
from ..models import ApplyResult, Status, Vacancy
from .base import JobProvider
from .hh_auth import AuthError, TokenStore, interactive_authorize
from .hh_client import HHApiError, HHClient
from .parsing import html_to_text, parse_datetime

# Требуемый опыт hh.ru в общей шкале уровней. hh.ru описывает не грейд, а
# требуемый стаж, поэтому «без опыта» приравнивается к junior, а не к стажёру.
EXPERIENCE_TO_SENIORITY = {
    "noExperience": "junior",
    "between1And3": "middle",
    "between3And6": "senior",
    "moreThan6": "lead",
}

# Параметры, которые можно указывать в config.yaml → hh.searches[].params
KNOWN_PARAMS = {
    "text",
    "area",
    "professional_role",
    "experience",
    "schedule",
    "employment",
    "period",
    "only_with_salary",
    "salary",
    "currency",
    "industry",
    "employer_id",
    "label",
    "order_by",
    "search_field",
}


class HHProvider(JobProvider):
    name = "hh"
    title = "hh.ru"
    can_apply = True
    known_params = frozenset(KNOWN_PARAMS)

    def __init__(self, config: Config):
        super().__init__(config)
        self.token_store = TokenStore(TOKENS_PATH, provider=self.name)
        self._request_delay = float(config.settings(self.name).get("request_delay", 0.5))

    def _build_client(self) -> HHClient:
        return HHClient(
            token_store=self.token_store,
            user_agent=self.config.secrets.hh_user_agent,
            request_delay=self._request_delay,
        )

    # --- авторизация ----------------------------------------------------

    def is_authorized(self) -> bool:
        if not self.client.has_tokens:
            return False
        try:
            self.client.me()
            return True
        except (HHApiError, AuthError):
            return False

    def authorization_hint(self) -> str:
        return "Выполни `python -m autoapply auth`."

    def authorize(self) -> str:
        secrets = self.config.secrets
        if not secrets.hh_client_id or not secrets.hh_client_secret:
            raise AuthError(
                "Не заданы HH_CLIENT_ID / HH_CLIENT_SECRET.\n"
                "Зарегистрируй приложение на https://dev.hh.ru/admin, укажи\n"
                f"Redirect URI = {secrets.hh_redirect_uri}\n"
                "и впиши выданные значения в файл .env (см. .env.example)."
            )
        tokens = interactive_authorize(
            client_id=secrets.hh_client_id,
            client_secret=secrets.hh_client_secret,
            redirect_uri=secrets.hh_redirect_uri,
            user_agent=secrets.hh_user_agent,
        )
        self.client.set_tokens(tokens)
        me = self.client.me()
        return " ".join(
            p for p in (me.get("first_name"), me.get("last_name")) if p
        ) or me.get("email", "аккаунт hh.ru")

    def whoami(self) -> dict:
        me = self.client.me()
        resumes = self.client.my_resumes()
        return {
            "account": me,
            "resumes": [
                {
                    "id": r.get("id"),
                    "title": r.get("title"),
                    "updated_at": r.get("updated_at"),
                    "status": (r.get("status") or {}).get("name"),
                    "url": r.get("alternate_url"),
                }
                for r in (resumes.get("items") or [])
            ],
        }

    # --- поиск ----------------------------------------------------------

    def search(self, spec: SearchSpec) -> Iterator[Vacancy]:
        base = {k: v for k, v in spec.params.items() if v is not None}
        base.setdefault("order_by", "publication_time")
        if isinstance(base.get("only_with_salary"), bool):
            base["only_with_salary"] = str(base["only_with_salary"]).lower()

        page = 0
        while page < spec.max_pages:
            payload = self.client.search_vacancies({**base, "per_page": 50, "page": page})
            items = payload.get("items") or []
            for item in items:
                yield self._to_vacancy(item)

            total_pages = int(payload.get("pages") or 0)
            page += 1
            if page >= total_pages or not items:
                return

    def _to_vacancy(self, item: dict) -> Vacancy:
        salary = item.get("salary") or {}
        area = item.get("area") or {}
        snippet = item.get("snippet") or {}
        description = "\n".join(
            html_to_text(snippet.get(k)) for k in ("requirement", "responsibility")
        ).strip()
        experience = (item.get("experience") or {}).get("id")
        schedule = (item.get("schedule") or {}).get("id")

        return Vacancy(
            provider=self.name,
            external_id=str(item.get("id")),
            title=item.get("name") or "",
            company=(item.get("employer") or {}).get("name") or "",
            url=item.get("alternate_url") or f"https://hh.ru/vacancy/{item.get('id')}",
            description=description,
            area_id=str(area.get("id")) if area.get("id") is not None else None,
            area_name=area.get("name"),
            salary_from=salary.get("from"),
            salary_to=salary.get("to"),
            salary_currency=salary.get("currency"),
            experience=experience,
            seniority=EXPERIENCE_TO_SENIORITY.get(experience or ""),
            schedule=schedule,
            employment=(item.get("employment") or {}).get("id"),
            published_at=parse_datetime(item.get("published_at")),
            remote=schedule == "remote",
            has_test=bool(item.get("has_test")),
            letter_required=bool(item.get("response_letter_required")),
            raw=item,
        )

    def fetch_details(self, vacancy: Vacancy) -> Vacancy:
        """Забрать полное описание и key_skills — на них держится скоринг."""
        try:
            item = self.client.vacancy(vacancy.external_id)
        except HHApiError:
            return vacancy  # останется скоринг по сниппету

        vacancy.description = html_to_text(item.get("description")) or vacancy.description
        vacancy.key_skills = [
            s.get("name", "") for s in (item.get("key_skills") or []) if s.get("name")
        ]
        vacancy.has_test = bool(item.get("has_test", vacancy.has_test))
        vacancy.letter_required = bool(
            item.get("response_letter_required", vacancy.letter_required)
        )
        if item.get("experience"):
            vacancy.experience = item["experience"].get("id", vacancy.experience)
            vacancy.seniority = EXPERIENCE_TO_SENIORITY.get(
                vacancy.experience or "", vacancy.seniority
            )
        vacancy.raw = {**vacancy.raw, "detail": item}
        return vacancy

    # --- отклик ---------------------------------------------------------

    def apply(self, vacancy: Vacancy, letter: str) -> ApplyResult:
        try:
            resume_id = self.config.require_hh_resume_id()
        except Exception as exc:  # noqa: BLE001 - пробрасываем текст пользователю
            return ApplyResult(Status.FAILED, str(exc))

        try:
            result = self.client.apply(vacancy.external_id, resume_id, letter)
        except HHApiError as exc:
            return self._classify_error(exc)
        except AuthError as exc:
            return ApplyResult(Status.FAILED, str(exc))

        return ApplyResult(
            status=Status.SENT,
            detail="отклик отправлен",
            negotiation_id=str(result.get("id")) if result.get("id") else None,
        )

    def _classify_error(self, exc: HHApiError) -> ApplyResult:
        """Развести окончательные отказы, повторяемые сбои и конец лимита."""
        if exc.has("already_applied"):
            return ApplyResult(Status.ALREADY, "отклик уже существует")
        if exc.has("limit_exceeded"):
            return ApplyResult(
                Status.QUEUED, "суточный лимит откликов исчерпан", limit_reached=True
            )
        if exc.has("test_required"):
            return ApplyResult(Status.UNAVAILABLE, "требуется тест — через API нельзя")
        if exc.has("invalid_vacancy", "vacancy_not_found"):
            return ApplyResult(Status.UNAVAILABLE, "вакансия в архиве или скрыта")
        if exc.has("resume_visibility_conflict"):
            return ApplyResult(Status.UNAVAILABLE, "конфликт видимости резюме")
        if exc.has("resume_not_found", "resume_deleted"):
            return ApplyResult(
                Status.FAILED, "резюме не найдено или скрыто — проверь hh.resume_id"
            )
        return ApplyResult(Status.FAILED, str(exc))
