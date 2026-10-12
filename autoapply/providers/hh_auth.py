"""OAuth 2.0 для hh.ru: получение, хранение и обновление токенов.

hh.ru использует authorization_code flow. Бот поднимает локальный сервер на
redirect_uri, открывает браузер и перехватывает `code` из редиректа.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import webbrowser
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

AUTHORIZE_URL = "https://hh.ru/oauth/authorize"

# hh.ru за годы переносил token-эндпоинт. Пробуем по очереди: первый ответивший
# осмысленно и запоминаем в файле токенов, чтобы не перебирать каждый раз.
TOKEN_URL_CANDIDATES = (
    "https://api.hh.ru/token",
    "https://hh.ru/oauth/token",
    "https://api.hh.ru/oauth/token",
)


class AuthError(RuntimeError):
    """Авторизация не удалась или токен непригоден."""


@dataclass
class Tokens:
    access_token: str
    refresh_token: str | None = None
    expires_at: str | None = None  # ISO 8601, UTC
    token_url: str | None = None   # какой эндпоинт сработал

    def is_expired(self, skew_seconds: int = 120) -> bool:
        if not self.expires_at:
            return False
        try:
            deadline = datetime.fromisoformat(self.expires_at)
        except ValueError:
            return True
        return datetime.now(timezone.utc) >= deadline - timedelta(seconds=skew_seconds)


class TokenStore:
    """Токены в JSON-файле рядом с БД. Файл в .gitignore."""

    def __init__(self, path: Path, provider: str = "hh"):
        self.path = path
        self.provider = provider

    def _read_all(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def load(self) -> Tokens | None:
        blob = self._read_all().get(self.provider)
        if not blob or not blob.get("access_token"):
            return None
        return Tokens(
            access_token=blob["access_token"],
            refresh_token=blob.get("refresh_token"),
            expires_at=blob.get("expires_at"),
            token_url=blob.get("token_url"),
        )

    def save(self, tokens: Tokens) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        blob = self._read_all()
        blob[self.provider] = asdict(tokens)
        self.path.write_text(
            json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # Токен даёт полный доступ к аккаунту — прячем от других пользователей ОС.
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def clear(self) -> None:
        blob = self._read_all()
        blob.pop(self.provider, None)
        self.path.write_text(
            json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8"
        )


class _CallbackHandler(BaseHTTPRequestHandler):
    """Одноразовый обработчик редиректа с hh.ru."""

    result: dict = {}
    expected_state: str = ""

    def do_GET(self) -> None:  # noqa: N802 - имя задано BaseHTTPRequestHandler
        query = parse_qs(urlparse(self.path).query)
        code = query.get("code", [None])[0]
        state = query.get("state", [None])[0]
        error = query.get("error_description", query.get("error", [None]))[0]

        if error:
            _CallbackHandler.result = {"error": error}
            body, status = f"Ошибка авторизации: {error}", 400
        elif not code:
            body, status = "Ожидался параметр ?code=...", 400
        elif state != _CallbackHandler.expected_state:
            _CallbackHandler.result = {"error": "state mismatch"}
            body, status = "Не совпал state — возможна подмена запроса.", 400
        else:
            _CallbackHandler.result = {"code": code}
            body, status = "Авторизация прошла. Можно закрыть вкладку.", 200

        payload = (
            f"<html><head><meta charset='utf-8'><title>autoapply</title></head>"
            f"<body style='font-family:sans-serif;padding:40px'>"
            f"<h2>{body}</h2></body></html>"
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:
        """Заглушить лог HTTPServer в stdout."""


def _wait_for_code(redirect_uri: str, state: str, timeout: float) -> str:
    parsed = urlparse(redirect_uri)
    host, port = parsed.hostname or "localhost", parsed.port or 80

    _CallbackHandler.result = {}
    _CallbackHandler.expected_state = state
    try:
        server = HTTPServer((host, port), _CallbackHandler)
    except OSError as exc:
        raise AuthError(
            f"Не удалось занять {host}:{port} для приёма редиректа ({exc}).\n"
            "Освободи порт или поменяй HH_REDIRECT_URI в .env "
            "(и тот же адрес в настройках приложения на dev.hh.ru)."
        ) from exc

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout)
        while datetime.now(timezone.utc) < deadline:
            if _CallbackHandler.result:
                break
            threading.Event().wait(0.25)
    finally:
        server.shutdown()
        server.server_close()

    result = _CallbackHandler.result
    if not result:
        raise AuthError(f"Не дождались редиректа за {timeout:.0f} с.")
    if "error" in result:
        raise AuthError(f"hh.ru отказал в авторизации: {result['error']}")
    return result["code"]


def _post_token(client: httpx.Client, url: str, data: dict) -> dict:
    response = client.post(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if response.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"{response.status_code}: {response.text[:400]}",
            request=response.request,
            response=response,
        )
    return response.json()


def _exchange(data: dict, user_agent: str, known_url: str | None = None) -> Tokens:
    """Обменять code или refresh_token на токены, подобрав рабочий эндпоинт."""
    candidates = (known_url,) if known_url else TOKEN_URL_CANDIDATES
    errors: list[str] = []

    with httpx.Client(
        timeout=30.0, headers={"User-Agent": user_agent}, follow_redirects=True
    ) as client:
        for url in candidates:
            if not url:
                continue
            try:
                payload = _post_token(client, url, data)
            except (httpx.HTTPStatusError, httpx.HTTPError, ValueError) as exc:
                errors.append(f"{url} -> {exc}")
                continue

            access = payload.get("access_token")
            if not access:
                errors.append(f"{url} -> в ответе нет access_token: {payload}")
                continue

            expires_at = None
            if isinstance(payload.get("expires_in"), (int, float)):
                expires_at = (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=int(payload["expires_in"]))
                ).isoformat(timespec="seconds")

            return Tokens(
                access_token=access,
                refresh_token=payload.get("refresh_token"),
                expires_at=expires_at,
                token_url=url,
            )

    raise AuthError("Не удалось получить токен.\n" + "\n".join(errors))


def build_authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def interactive_authorize(
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    user_agent: str,
    timeout: float = 300.0,
    open_browser: bool = True,
) -> Tokens:
    """Полный цикл: браузер -> code -> токены."""
    state = secrets.token_urlsafe(16)
    url = build_authorize_url(client_id, redirect_uri, state)

    print("Открываю страницу авторизации hh.ru.")
    print("Если браузер не открылся, перейди по ссылке вручную:\n")
    print(f"  {url}\n")
    if open_browser:
        webbrowser.open(url)

    code = _wait_for_code(redirect_uri, state, timeout)
    return _exchange(
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        },
        user_agent=user_agent,
    )


def refresh(tokens: Tokens, user_agent: str) -> Tokens:
    """Обновить access_token по refresh_token.

    hh.ru инвалидирует refresh_token после использования, поэтому результат
    обязательно нужно сохранить.
    """
    if not tokens.refresh_token:
        raise AuthError("Нет refresh_token — нужна повторная авторизация: `auth`.")
    refreshed = _exchange(
        {"grant_type": "refresh_token", "refresh_token": tokens.refresh_token},
        user_agent=user_agent,
        known_url=tokens.token_url,
    )
    # Если hh.ru не вернул новый refresh_token, оставляем прежний.
    if not refreshed.refresh_token:
        refreshed.refresh_token = tokens.refresh_token
    return refreshed
