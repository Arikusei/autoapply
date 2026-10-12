"""Загрузка конфигурации, профиля кандидата и секретов."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CONFIG_PATH = ROOT / "config.yaml"
PROFILE_PATH = ROOT / "profile.yaml"
TOKENS_PATH = DATA_DIR / "tokens.json"
DB_PATH = DATA_DIR / "state.sqlite3"


class ConfigError(RuntimeError):
    """Конфигурация неполная или противоречивая."""


@dataclass(frozen=True)
class SearchSpec:
    """Один поисковый запрос.

    `params` передаются провайдеру как есть — у каждой площадки свои имена
    параметров, и подменять их обобщёнными означало бы терять возможности API.
    Что именно поддерживает площадка, описано в README.
    """

    name: str
    provider: str
    params: dict[str, Any] = field(default_factory=dict)
    max_pages: int = 10


@dataclass(frozen=True)
class ScoringSpec:
    threshold: int = 6
    require_any: list[str] = field(default_factory=list)
    weights: dict[str, int] = field(default_factory=dict)
    blocklist: list[str] = field(default_factory=list)
    reject_title_patterns: list[str] = field(default_factory=list)
    allow_seniority: list[str] = field(default_factory=list)
    bonuses: dict[str, int] = field(default_factory=dict)
    home_area_names: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LettersSpec:
    mode: str = "llm"
    llm_provider: str = "openai"
    model: str = "gpt-4o-mini"
    max_chars: int = 1500
    temperature: float = 0.7
    fallback_to_template: bool = True
    template: str = ""


@dataclass(frozen=True)
class SendingSpec:
    daily_limit: int = 100
    per_run_limit: int = 20
    delay_between: float = 4.0


@dataclass(frozen=True)
class Secrets:
    """Значения из .env. Никогда не попадают в config.yaml."""

    hh_client_id: str | None = None
    hh_client_secret: str | None = None
    hh_redirect_uri: str = "http://localhost:8765/callback"
    hh_user_agent: str = "autoapply/1.0 (job-search-bot)"
    # Habr Career и getmatch не дают соискателю API для отклика, поэтому
    # единственный путь — cookie из собственного браузера. Подробности в README.
    habr_remember_token: str | None = None
    habr_session: str | None = None
    getmatch_session: str | None = None
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    anthropic_api_key: str | None = None
    anthropic_base_url: str = "https://api.anthropic.com/v1"


@dataclass(frozen=True)
class Config:
    providers: list[str]
    provider_settings: dict[str, dict[str, Any]]
    searches: list[SearchSpec]
    scoring: ScoringSpec
    letters: LettersSpec
    sending: SendingSpec
    profile: dict[str, Any]
    secrets: Secrets

    def settings(self, provider: str) -> dict[str, Any]:
        return self.provider_settings.get(provider) or {}

    def searches_for(self, provider: str) -> list[SearchSpec]:
        return [s for s in self.searches if s.provider == provider]

    @property
    def hh_resume_id(self) -> str | None:
        return self.settings("hh").get("resume_id")

    def require_hh_resume_id(self) -> str:
        rid = self.hh_resume_id
        if not rid:
            raise ConfigError(
                "В config.yaml не задан hh.resume_id.\n"
                "Выполни `python -m autoapply whoami`, скопируй id нужного резюме "
                "и впиши его в config.yaml → hh.resume_id."
            )
        return str(rid)


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def load_secrets() -> Secrets:
    load_dotenv(ROOT / ".env")
    env = os.environ
    defaults = Secrets()
    return Secrets(
        hh_client_id=env.get("HH_CLIENT_ID") or None,
        hh_client_secret=env.get("HH_CLIENT_SECRET") or None,
        hh_redirect_uri=env.get("HH_REDIRECT_URI") or defaults.hh_redirect_uri,
        hh_user_agent=env.get("HH_USER_AGENT") or defaults.hh_user_agent,
        habr_remember_token=env.get("HABR_REMEMBER_TOKEN") or None,
        habr_session=env.get("HABR_SESSION") or None,
        getmatch_session=env.get("GETMATCH_SESSION") or None,
        openai_api_key=env.get("OPENAI_API_KEY") or None,
        openai_base_url=env.get("OPENAI_BASE_URL") or defaults.openai_base_url,
        anthropic_api_key=env.get("ANTHROPIC_API_KEY") or None,
        anthropic_base_url=env.get("ANTHROPIC_BASE_URL") or defaults.anthropic_base_url,
    )


def _load_searches(raw: dict, providers: list[str]) -> list[SearchSpec]:
    """Собрать запросы из блоков всех включённых площадок."""
    searches: list[SearchSpec] = []
    for provider in providers:
        block = raw.get(provider) or {}
        for index, entry in enumerate(_as_list(block.get("searches"))):
            if not isinstance(entry, dict):
                raise ConfigError(
                    f"{provider}.searches[{index}] должен быть словарём с полями "
                    "name и params."
                )
            params = entry.get("params") or {}
            if not isinstance(params, dict):
                raise ConfigError(
                    f"{provider}.searches[{index}].params должен быть словарём."
                )
            searches.append(
                SearchSpec(
                    name=entry.get("name") or f"{provider}-{index + 1}",
                    provider=provider,
                    params=dict(params),
                    max_pages=int(entry.get("max_pages", 10)),
                )
            )
    return searches


def load_config(
    config_path: Path | None = None, profile_path: Path | None = None
) -> Config:
    config_path = config_path or CONFIG_PATH
    profile_path = profile_path or PROFILE_PATH

    if not config_path.exists():
        raise ConfigError(f"Не найден файл конфигурации: {config_path}")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    profile: dict[str, Any] = {}
    if profile_path.exists():
        profile = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}

    providers = [str(p) for p in _as_list(raw.get("providers"))]
    if not providers:
        raise ConfigError(
            "В config.yaml не включена ни одна площадка. Пример:\n"
            "providers: [hh, habr, getmatch]"
        )

    searches = _load_searches(raw, providers)
    if not searches:
        listed = ", ".join(providers)
        raise ConfigError(
            f"Ни у одной включённой площадки ({listed}) нет запросов. "
            "Добавь блок searches, например в config.yaml → hh.searches."
        )

    sc = raw.get("scoring") or {}
    scoring = ScoringSpec(
        threshold=int(sc.get("threshold", 6)),
        require_any=[str(x).lower() for x in _as_list(sc.get("require_any"))],
        weights={str(k).lower(): int(v) for k, v in (sc.get("weights") or {}).items()},
        blocklist=[str(x).lower() for x in _as_list(sc.get("blocklist"))],
        reject_title_patterns=[
            str(x).lower() for x in _as_list(sc.get("reject_title_patterns"))
        ],
        allow_seniority=[str(x).lower() for x in _as_list(sc.get("allow_seniority"))],
        bonuses={str(k): int(v) for k, v in (sc.get("bonuses") or {}).items()},
        home_area_names=[
            str(x).lower() for x in _as_list(sc.get("home_area_names"))
        ],
    )

    lt = raw.get("letters") or {}
    letters = LettersSpec(
        mode=str(lt.get("mode", "llm")),
        llm_provider=str(lt.get("llm_provider", "openai")),
        model=str(lt.get("model", "gpt-4o-mini")),
        max_chars=int(lt.get("max_chars", 1500)),
        temperature=float(lt.get("temperature", 0.7)),
        fallback_to_template=bool(lt.get("fallback_to_template", True)),
        template=str(lt.get("template", "")),
    )

    sn = raw.get("sending") or {}
    sending = SendingSpec(
        daily_limit=int(sn.get("daily_limit", 100)),
        per_run_limit=int(sn.get("per_run_limit", 20)),
        delay_between=float(sn.get("delay_between", 4.0)),
    )

    return Config(
        providers=providers,
        provider_settings={p: dict(raw.get(p) or {}) for p in providers},
        searches=searches,
        scoring=scoring,
        letters=letters,
        sending=sending,
        profile=profile,
        secrets=load_secrets(),
    )
