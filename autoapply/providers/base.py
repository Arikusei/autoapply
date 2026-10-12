"""Контракт провайдера вакансий.

Чтобы добавить площадку, реализуй этот интерфейс и зарегистрируй класс в
providers/__init__.py. Скоринг, письма, очередь и CLI не меняются — они работают
с провайдеро-независимой моделью Vacancy.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from ..config import Config, SearchSpec
from ..models import ApplyResult, Status, Vacancy


class JobProvider(ABC):
    """Площадка с вакансиями."""

    # Короткий идентификатор, он же ключ в config.yaml и префикс в БД.
    name: str = "base"
    # Человекочитаемое название для вывода.
    title: str = "площадка"
    # Умеет ли площадка отправлять отклик из бота. Если нет, вакансии
    # попадают в очередь со статусом manual: письмо готово, отклик руками.
    can_apply: bool = False
    # Параметры, допустимые в config.yaml → <площадка>.searches[].params
    known_params: frozenset[str] = frozenset()

    def __init__(self, config: Config):
        self.config = config
        self._client = None

    # HTTP-клиент создаётся при первом обращении: построение SSL-контекста
    # заметно дороже самого объекта, а команды вроде `providers` и `status`
    # сеть вообще не трогают.
    @property
    def client(self):
        if self._client is None:
            self._client = self._build_client()
        return self._client

    @client.setter
    def client(self, value) -> None:
        self._client = value

    def _build_client(self):
        raise NotImplementedError

    # --- авторизация ----------------------------------------------------

    @abstractmethod
    def is_authorized(self) -> bool:
        """Есть ли действующие учётные данные для отклика."""

    @abstractmethod
    def authorization_hint(self) -> str:
        """Что сделать пользователю, чтобы появился доступ к отклику."""

    def authorize(self) -> str:
        """Интерактивная авторизация. Не у всех площадок есть OAuth."""
        raise NotImplementedError(
            f"{self.title} не поддерживает интерактивную авторизацию. "
            + self.authorization_hint()
        )

    def whoami(self) -> dict:
        """Данные аккаунта. Поддерживается только там, где есть API аккаунта."""
        raise NotImplementedError(f"{self.title} не отдаёт данные аккаунта через API.")

    # --- поиск ----------------------------------------------------------

    @abstractmethod
    def search(self, spec: SearchSpec) -> Iterator[Vacancy]:
        """Вакансии по запросу. Описание может быть неполным."""

    def fetch_details(self, vacancy: Vacancy) -> Vacancy:
        """Дополнить вакансию описанием и навыками для точного скоринга."""
        return vacancy

    # --- отклик ---------------------------------------------------------

    def apply(self, vacancy: Vacancy, letter: str) -> ApplyResult:
        """Отправить отклик.

        По умолчанию площадка этого не умеет: вакансия помечается как
        требующая ручного отклика, письмо остаётся в очереди.
        """
        return ApplyResult(
            status=Status.MANUAL,
            detail=f"{self.title}: отклик только вручную — {vacancy.url}",
        )

    # --- вспомогательное ------------------------------------------------

    def unknown_params(self, spec: SearchSpec) -> list[str]:
        """Незнакомые ключи в params.

        Опечатка в имени фильтра не вызывает ошибку у API — она молча меняет
        выдачу, поэтому о таких ключах нужно предупреждать.
        """
        if not self.known_params:
            return []
        return sorted(set(spec.params) - set(self.known_params))
