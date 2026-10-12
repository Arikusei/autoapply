"""Реестр провайдеров вакансий."""

from __future__ import annotations

from ..config import Config
from .base import JobProvider
from .getmatch_provider import GetmatchProvider
from .habr_provider import HabrProvider
from .hh_provider import HHProvider

# Новая площадка добавляется одной строкой здесь.
PROVIDERS: dict[str, type[JobProvider]] = {
    HHProvider.name: HHProvider,
    HabrProvider.name: HabrProvider,
    GetmatchProvider.name: GetmatchProvider,
}


def get_provider(config: Config, name: str) -> JobProvider:
    try:
        provider_cls = PROVIDERS[name]
    except KeyError:
        known = ", ".join(sorted(PROVIDERS))
        raise ValueError(
            f"Неизвестная площадка '{name}'. Доступны: {known}."
        ) from None
    return provider_cls(config)


def get_providers(config: Config, only: str | None = None) -> list[JobProvider]:
    """Провайдеры включённых площадок, либо одна запрошенная."""
    names = config.providers
    if only:
        if only not in names:
            enabled = ", ".join(names)
            raise ValueError(
                f"Площадка '{only}' не включена в config.yaml. Включены: {enabled}."
            )
        names = [only]
    return [get_provider(config, name) for name in names]


__all__ = [
    "JobProvider",
    "HHProvider",
    "HabrProvider",
    "GetmatchProvider",
    "PROVIDERS",
    "get_provider",
    "get_providers",
]
