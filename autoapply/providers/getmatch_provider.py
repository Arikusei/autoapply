"""Провайдер getmatch.

У getmatch нет публичного API и нет API-ключей: вход только по одноразовому коду
на email или в Telegram. Поиск идёт через открытый `/api/offers` — тот же
эндпоинт, что использует сайт. Отклик требует cookie сессии из браузера и
включается явно; по умолчанию вакансии попадают в очередь с готовым письмом,
а отклик делается руками по ссылке.
"""

from __future__ import annotations

from collections.abc import Iterator

from ..config import Config, SearchSpec
from ..models import ApplyResult, Status, Vacancy, lowest_seniority
from .base import JobProvider
from .http import BROWSER_USER_AGENT, ApiError, ThrottledClient
from .parsing import html_to_text, parse_datetime

BASE_URL = "https://getmatch.ru"

# Уровни getmatch в общей шкале.
SENIORITY_MAP = {
    "intern": "intern",
    "junior": "junior",
    "middle": "middle",
    "senior": "senior",
    "lead": "lead",
    "c_level": "lead",
}

# Параметры для config.yaml → getmatch.searches[].params
#   sa — минимальная зарплата (any, 150000, 200000, 250000, 350000)
#   pa — период публикации (all, 30d, 7d, 3d, 1d)
#   l  — локация и формат (moscow, saints_p, remote, relocate, regions_ru)
#   se — уровень (junior, middle, senior, lead, c_level)
#   sp — специализация (слаги из GET /api/specializations)
KNOWN_PARAMS = {"sa", "pa", "l", "se", "sp", "s", "c", "pl"}

PAGE_SIZE = 30

# Кроме вакансий выдача содержит рекрутинговые события (one_day_offer_*),
# на которые нельзя откликнуться как на вакансию.
VACANCY_OFFER_TYPE = "vacancy"


class GetmatchApiError(ApiError):
    """Ошибка getmatch."""


class GetmatchClient(ThrottledClient):
    error_cls = GetmatchApiError

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

    def offers(self, params: list[tuple[str, str]]) -> dict:
        return self.request("GET", "/api/offers", params=params)

    def offer(self, offer_id: str) -> dict:
        return self.request("GET", f"/api/offers/{offer_id}")

    def specializations(self) -> list[dict]:
        payload = self.request("GET", "/api/specializations")
        if isinstance(payload, list):
            return payload
        return (payload or {}).get("specializations") or []

    def apply(self, offer_id: str, letter: str) -> dict:
        """Отправить отклик.

        Форма отклика не документирована, поэтому пробуем форматы по очереди:
        сначала multipart (так делает сайт), затем JSON, затем urlencoded.
        """
        attempts = (
            ("multipart", {"files": {"cover_letter": (None, letter)}}),
            ("json", {"json_body": {"cover_letter": letter}}),
            ("form", {"data": {"cover_letter": letter}}),
        )
        last_error: GetmatchApiError | None = None
        for _, kwargs in attempts:
            try:
                result = self.request("POST", f"/api/offers/{offer_id}/apply", **kwargs)
            except GetmatchApiError as exc:
                # Смысловые отказы формат не исправит.
                if exc.status_code not in (400, 415, 422):
                    raise
                last_error = exc
                continue
            return result if isinstance(result, dict) else {}
        raise last_error or GetmatchApiError(0, None, "Не удалось отправить отклик.")


def _flatten_params(params: dict) -> list[tuple[str, str]]:
    """Многозначные фильтры getmatch передаются повторением параметра."""
    flat: list[tuple[str, str]] = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            flat.extend((key, str(v)) for v in value)
        elif isinstance(value, bool):
            flat.append((key, "true" if value else "false"))
        else:
            flat.append((key, str(value)))
    return flat


class GetmatchProvider(JobProvider):
    name = "getmatch"
    title = "getmatch"
    known_params = frozenset(KNOWN_PARAMS)

    def __init__(self, config: Config):
        super().__init__(config)
        settings = config.settings(self.name)
        session = config.secrets.getmatch_session
        self._cookies = {"session": session} if session else {}
        self._request_delay = float(settings.get("request_delay", 1.0))
        self.apply_enabled = bool(settings.get("apply_enabled", False))

    def _build_client(self) -> GetmatchClient:
        return GetmatchClient(
            request_delay=self._request_delay, cookies=self._cookies
        )

    @property
    def can_apply(self) -> bool:  # type: ignore[override]
        return self.apply_enabled and bool(self._cookies)

    # --- авторизация ----------------------------------------------------

    def is_authorized(self) -> bool:
        return self.can_apply

    def authorization_hint(self) -> str:
        if not self.apply_enabled:
            return (
                "Автоотклик на getmatch выключен: у площадки нет API для отклика. "
                "Вакансии попадают в очередь с готовым письмом — откликайся по "
                "ссылке. Если всё же хочешь пробовать из бота, см. README."
            )
        return (
            "Не задана cookie сессии getmatch. Скопируй её из браузера в .env "
            "(GETMATCH_SESSION)."
        )

    # --- поиск ----------------------------------------------------------

    def search(self, spec: SearchSpec) -> Iterator[Vacancy]:
        base = dict(spec.params)
        base.setdefault("sa", "any")
        base.setdefault("pa", "all")

        offset = 0
        for _ in range(spec.max_pages):
            params = _flatten_params(
                {**base, "p": offset // PAGE_SIZE + 1, "offset": offset, "limit": PAGE_SIZE}
            )
            payload = self.client.offers(params)
            offers = (payload or {}).get("offers") or []
            if not offers:
                return

            for offer in offers:
                # Рекрутинговые события — не вакансии, откликаться не на что.
                if offer.get("offer_type") != VACANCY_OFFER_TYPE:
                    continue
                if offer.get("is_active") is False:
                    continue
                yield self._to_vacancy(offer)

            total = int(((payload or {}).get("meta") or {}).get("total") or 0)
            offset += PAGE_SIZE
            if offset >= total:
                return

    def _to_vacancy(self, offer: dict) -> Vacancy:
        company = offer.get("company") or {}
        # В slug иногда приходит None — фильтруем, иначе join упадёт.
        skills = [
            str(s.get("name") or s.get("slug"))
            for s in (offer.get("skills_objects") or [])
            if s and (s.get("name") or s.get("slug"))
        ]
        location_items = offer.get("location_items") or []
        locations = [loc.get("label") for loc in location_items if loc.get("label")]
        remote = any(loc.get("format") == "remote" for loc in location_items)

        levels = offer.get("seniorities") or (
            [offer["seniority"]] if offer.get("seniority") else []
        )
        mapped = [SENIORITY_MAP.get(str(lvl).lower()) for lvl in levels]
        seniority = lowest_seniority([m for m in mapped if m])

        description = html_to_text(
            offer.get("description_html")
            or offer.get("description")
            or offer.get("offer_description")
        )
        if offer.get("stack_description"):
            description = f"{description}\n\n{html_to_text(offer['stack_description'])}"

        salary_hidden = bool(offer.get("salary_hidden"))
        return Vacancy(
            provider=self.name,
            external_id=str(offer.get("id")),
            title=offer.get("position") or "",
            company=company.get("name") or "",
            url=BASE_URL + (offer.get("url") or f"/vacancies/{offer.get('id')}"),
            description=description,
            key_skills=skills,
            area_name=", ".join(locations) if locations else None,
            salary_from=None if salary_hidden else offer.get("salary_display_from"),
            salary_to=None if salary_hidden else offer.get("salary_display_to"),
            salary_currency=offer.get("salary_currency"),
            experience=offer.get("seniority"),
            seniority=seniority,
            published_at=parse_datetime(offer.get("published_at")),
            remote=remote,
            letter_required=bool(offer.get("cover_letter_required")),
            raw=offer,
        )

    def fetch_details(self, vacancy: Vacancy) -> Vacancy:
        """В списке приходит только тизер — полный текст лежит в карточке."""
        try:
            offer = self.client.offer(vacancy.external_id)
        except GetmatchApiError:
            return vacancy

        detailed = self._to_vacancy({**vacancy.raw, **offer})
        # Название и компания в списке точнее не станут, а описание — станет.
        detailed.raw = {**vacancy.raw, "detail": offer}
        return detailed

    # --- отклик ---------------------------------------------------------

    def apply(self, vacancy: Vacancy, letter: str) -> ApplyResult:
        if not self.can_apply:
            return ApplyResult(
                status=Status.MANUAL,
                detail=f"нужен ручной отклик — {vacancy.url}",
            )

        try:
            result = self.client.apply(vacancy.external_id, letter)
        except GetmatchApiError as exc:
            return self._classify_error(exc)

        return ApplyResult(
            status=Status.SENT,
            detail="отклик отправлен",
            negotiation_id=str(result.get("id")) if result.get("id") else None,
        )

    def _classify_error(self, exc: GetmatchApiError) -> ApplyResult:
        if exc.status_code in (401, 403):
            return ApplyResult(
                Status.QUEUED,
                "cookie сессии getmatch истекла — обнови GETMATCH_SESSION в .env",
                limit_reached=True,
            )
        if exc.has("already", "уже"):
            return ApplyResult(Status.ALREADY, "отклик уже существует")
        if exc.status_code == 404:
            return ApplyResult(Status.UNAVAILABLE, "вакансия снята или скрыта")
        return ApplyResult(Status.FAILED, str(exc))
