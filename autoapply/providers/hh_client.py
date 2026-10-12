"""Клиент API hh.ru: авторизация по токену поверх общего транспорта."""

from __future__ import annotations

from typing import Any

from .hh_auth import AuthError, Tokens, TokenStore, refresh
from .http import ApiError, ThrottledClient

BASE_URL = "https://api.hh.ru"


class HHApiError(ApiError):
    """Ошибка API hh.ru."""


class HHClient(ThrottledClient):
    error_cls = HHApiError

    def __init__(
        self,
        token_store: TokenStore,
        user_agent: str,
        request_delay: float = 0.5,
        timeout: float = 30.0,
    ):
        super().__init__(
            base_url=BASE_URL,
            user_agent=user_agent,
            request_delay=request_delay,
            timeout=timeout,
        )
        self.token_store = token_store
        self.user_agent = user_agent
        self._tokens: Tokens | None = token_store.load()
        # Какой формат тела приняла ручка отклика — определяется на первом отклике.
        self._apply_format: str | None = None

    # --- токены ---------------------------------------------------------

    @property
    def has_tokens(self) -> bool:
        return self._tokens is not None

    def set_tokens(self, tokens: Tokens) -> None:
        self._tokens = tokens
        self.token_store.save(tokens)

    def _default_headers(self) -> dict[str, str]:
        if not self._tokens:
            raise AuthError("Нет токена. Выполни `python -m autoapply auth`.")
        if self._tokens.is_expired():
            self._refresh_tokens()
        return {"Authorization": f"Bearer {self._tokens.access_token}"}

    def _refresh_tokens(self) -> None:
        if not self._tokens:
            raise AuthError("Нет токена. Выполни `python -m autoapply auth`.")
        self.set_tokens(refresh(self._tokens, self.user_agent))

    # --- транспорт ------------------------------------------------------

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Как в базовом классе, но с одной попыткой обновить токен на 403."""
        refreshed = kwargs.pop("_refreshed", False)
        try:
            return super().request(method, path, **kwargs)
        except HHApiError as exc:
            expired = exc.status_code == 403 and exc.has(
                "token_expired", "oauth", "invalid_token"
            )
            if not expired or refreshed:
                raise
            self._refresh_tokens()
            return super().request(method, path, **kwargs)

    # --- ручки API ------------------------------------------------------

    def me(self) -> dict:
        return self.request("GET", "/me")

    def my_resumes(self) -> dict:
        return self.request("GET", "/resumes/mine")

    def search_vacancies(self, params: dict) -> dict:
        return self.request("GET", "/vacancies", params=params)

    def vacancy(self, vacancy_id: str) -> dict:
        return self.request("GET", f"/vacancies/{vacancy_id}")

    def apply(self, vacancy_id: str, resume_id: str, message: str) -> dict:
        """Откликнуться на вакансию.

        hh.ru убрал схему этой ручки из публичной документации, а разные
        клиенты отправляют её по-разному. Поэтому первый отклик подбирает
        рабочий формат тела, а последующие используют уже найденный.
        """
        fields = {"vacancy_id": str(vacancy_id), "resume_id": str(resume_id)}
        attempts: list[tuple[str, dict, dict | None]] = [
            ("form", {**fields, "message": message}, None),
            ("json", {}, {**fields, "message": message}),
            ("form-letter", {**fields, "letter": message}, None),
        ]
        if self._apply_format:
            attempts.sort(key=lambda a: a[0] != self._apply_format)

        last_error: HHApiError | None = None
        for fmt, form, body in attempts:
            try:
                result = self.request(
                    "POST", "/negotiations", data=form or None, json_body=body
                )
            except HHApiError as exc:
                # Ошибки по смыслу вакансии формат не изменит — пробрасываем сразу.
                if exc.status_code != 400 or exc.has(
                    "already_applied",
                    "limit_exceeded",
                    "test_required",
                    "invalid_vacancy",
                    "vacancy_not_found",
                    "resume_not_found",
                    "resume_visibility_conflict",
                ):
                    raise
                last_error = exc
                continue
            self._apply_format = fmt
            return result if isinstance(result, dict) else {}

        raise last_error or HHApiError(0, None, "Не удалось отправить отклик.")
