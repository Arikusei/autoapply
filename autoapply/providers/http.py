"""Общий HTTP-транспорт для провайдеров: троттлинг, ретраи, разбор ошибок.

Все три площадки требуют одного и того же: не долбить API чаще разрешённого,
переживать 429/5xx и приводить тело ошибки к машиночитаемому виду.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

# Коды, при которых имеет смысл повторить запрос.
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 4
MAX_RETRY_DELAY = 60.0

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


class ApiError(RuntimeError):
    """Ошибка API с разобранным телом ответа."""

    def __init__(self, status_code: int, body: Any, message: str = ""):
        self.status_code = status_code
        self.body = body
        self.error_types = self._extract(body)
        super().__init__(
            message or f"HTTP {status_code}: {', '.join(self.error_types) or body}"
        )

    @staticmethod
    def _extract(body: Any) -> list[str]:
        """Машиночитаемые причины отказа из тела ответа.

        Форматы различаются: hh.ru отдаёт {"errors":[{"type","value"}]},
        Habr и getmatch — {"message":...} или {"error":{"type":...}}.
        """
        if not isinstance(body, dict):
            return []
        values: list[str] = []
        for err in body.get("errors") or []:
            if isinstance(err, dict):
                values.extend(str(err[k]) for k in ("value", "type") if err.get(k))
            elif isinstance(err, str):
                values.append(err)
        error = body.get("error")
        if isinstance(error, dict):
            values.extend(str(error[k]) for k in ("type", "code", "message") if error.get(k))
        elif error:
            values.append(str(error))
        for key in ("error_description", "description", "message", "detail"):
            if body.get(key):
                values.append(str(body[key]))
        return values

    def has(self, *names: str) -> bool:
        """Есть ли среди причин хотя бы одна из указанных (подстрокой)."""
        lowered = [v.lower() for v in self.error_types]
        return any(n.lower() in v for n in names for v in lowered)


def parse_body(response: httpx.Response) -> Any:
    """JSON, если это JSON, иначе обрезанный текст."""
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text[:500]


class ThrottledClient:
    """HTTP-клиент с паузой между запросами и ретраями."""

    error_cls: type[ApiError] = ApiError

    def __init__(
        self,
        base_url: str,
        user_agent: str,
        request_delay: float = 0.5,
        timeout: float = 30.0,
        cookies: dict[str, str] | None = None,
    ):
        self.request_delay = request_delay
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={"User-Agent": user_agent},
            cookies=cookies or {},
            follow_redirects=True,
        )
        self._last_request_at = 0.0

    def close(self) -> None:
        self._client.close()

    def _default_headers(self) -> dict[str, str]:
        """Переопределяется наследниками для авторизации."""
        return {}

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.request_delay:
            time.sleep(self.request_delay - elapsed)
        self._last_request_at = time.monotonic()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        data: dict | None = None,
        json_body: Any = None,
        files: Any = None,
        headers: dict[str, str] | None = None,
        raw: bool = False,
    ) -> Any:
        merged = {**self._default_headers(), **(headers or {})}
        last_exc: Exception | None = None

        for attempt in range(MAX_RETRIES):
            self._throttle()
            try:
                response = self._client.request(
                    method,
                    path,
                    params=params,
                    data=data,
                    json=json_body,
                    files=files,
                    headers=merged,
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                time.sleep(min(2**attempt, MAX_RETRY_DELAY))
                continue

            if response.status_code in RETRY_STATUSES:
                retry_after = response.headers.get("Retry-After")
                delay = (
                    float(retry_after)
                    if retry_after and retry_after.isdigit()
                    else 2**attempt
                )
                last_exc = self.error_cls(response.status_code, parse_body(response))
                time.sleep(min(delay, MAX_RETRY_DELAY))
                continue

            if response.status_code >= 400:
                raise self.error_cls(response.status_code, parse_body(response))

            return response.text if raw else parse_body(response)

        raise self.error_cls(
            0, str(last_exc), f"Запрос {method} {path} не удался: {last_exc}"
        )
