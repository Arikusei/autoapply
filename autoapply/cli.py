"""Командный интерфейс бота автоотклика."""

from __future__ import annotations

import time
from collections import Counter
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

from .config import DB_PATH, Config, ConfigError, load_config
from .letters import LetterError, LetterWriter
from .models import Status, Vacancy
from .providers import JobProvider, get_providers
from .providers.hh_auth import AuthError
from .scoring import hard_filter, score_vacancy
from .storage import QueueItem, Storage

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Бот автоотклика на IT-вакансии (hh.ru, Habr Career, getmatch).",
)
console = Console()

STATUS_LABELS = {
    Status.QUEUED.value: "В очереди",
    Status.SENT.value: "Отправлено",
    Status.MANUAL.value: "Отклик вручную",
    Status.SKIPPED.value: "Отклонено скорингом",
    Status.FAILED.value: "Сбой отправки",
    Status.ALREADY.value: "Отклик уже был",
    Status.UNAVAILABLE.value: "Недоступны",
}

ProviderOption = Annotated[
    str | None,
    typer.Option("--provider", "-p", help="Работать только с одной площадкой."),
]


def _load() -> Config:
    try:
        return load_config()
    except ConfigError as exc:
        console.print(f"[red]Ошибка конфигурации:[/red] {exc}")
        raise typer.Exit(code=2) from exc


def _fail(message: str) -> None:
    console.print(f"[red]{message}[/red]")
    raise typer.Exit(code=1)


def _shorten(text: str, width: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _providers(config: Config, only: str | None) -> list[JobProvider]:
    try:
        return get_providers(config, only)
    except ValueError as exc:
        _fail(str(exc))
        raise


def _to_vacancy(item: QueueItem) -> Vacancy:
    """Минимальная Vacancy для отправки: провайдеру нужны id и ссылка."""
    return Vacancy(
        provider=item.provider,
        external_id=item.external_id,
        title=item.title,
        company=item.company,
        url=item.url,
        area_name=item.area_name,
    )


# --------------------------------------------------------------------------
# Авторизация и аккаунт
# --------------------------------------------------------------------------


@app.command()
def auth(provider: ProviderOption = "hh") -> None:
    """Авторизоваться на площадке (OAuth есть только у hh.ru)."""
    config = _load()
    for prov in _providers(config, provider):
        console.print(Rule(f"[bold]{prov.title}[/bold]"))
        try:
            who = prov.authorize()
        except NotImplementedError as exc:
            console.print(f"[yellow]{exc}[/yellow]")
            continue
        except AuthError as exc:
            console.print(f"[red]{exc}[/red]")
            continue
        console.print(f"[green]Готово.[/green] Авторизован как: {who}")
        console.print("Дальше: [bold]python -m autoapply whoami[/bold] — выбрать резюме.")


@app.command()
def whoami() -> None:
    """Показать аккаунт hh.ru и список резюме с их id."""
    config = _load()
    provider = _providers(config, "hh")[0]
    try:
        info = provider.whoami()
    except AuthError as exc:
        _fail(str(exc))
    except Exception as exc:  # noqa: BLE001 - причину показываем пользователю
        _fail(f"Не удалось получить данные аккаунта: {exc}")

    account = info.get("account") or {}
    name = " ".join(p for p in (account.get("first_name"), account.get("last_name")) if p)
    console.print(
        Panel(
            f"{name or '—'}\n{account.get('email') or ''}", title="Аккаунт", expand=False
        )
    )

    resumes = info.get("resumes") or []
    if not resumes:
        console.print("[yellow]Резюме не найдены — создай резюме на hh.ru.[/yellow]")
        return

    table = Table(title="Резюме", expand=False)
    table.add_column("id", style="cyan", no_wrap=True)
    table.add_column("Название")
    table.add_column("Статус")
    table.add_column("Обновлено", no_wrap=True)
    for resume in resumes:
        table.add_row(
            str(resume.get("id") or "—"),
            _shorten(str(resume.get("title") or "—"), 45),
            _shorten(str(resume.get("status") or "—"), 20),
            str(resume.get("updated_at") or "—")[:10],
        )
    console.print(table)

    current = config.hh_resume_id
    if current:
        console.print(f"\nВ config.yaml выбрано: [cyan]{current}[/cyan]")
    else:
        console.print("\n[yellow]Впиши нужный id в config.yaml → hh.resume_id[/yellow]")


@app.command()
def providers() -> None:
    """Показать площадки: включены ли и можно ли с них откликаться."""
    config = _load()
    table = Table(title="Площадки", expand=False)
    table.add_column("Площадка")
    table.add_column("Запросов", justify="right")
    table.add_column("Автоотклик")
    table.add_column("Что нужно")

    for prov in _providers(config, None):
        count = len(config.searches_for(prov.name))
        if prov.can_apply:
            state, hint = "[green]да[/green]", "—"
        else:
            state, hint = "[yellow]нет[/yellow]", prov.authorization_hint()
        table.add_row(prov.title, str(count), state, _shorten(hint, 60))
    console.print(table)


# --------------------------------------------------------------------------
# Поиск и наполнение очереди
# --------------------------------------------------------------------------


@app.command()
def scan(
    max_new: Annotated[
        int, typer.Option("--max-new", help="Сколько новых вакансий поставить в очередь.")
    ] = 25,
    no_letters: Annotated[
        bool,
        typer.Option("--no-letters", help="Не генерировать письма (не тратить токены)."),
    ] = False,
    provider: ProviderOption = None,
    search: Annotated[
        str | None, typer.Option("--search", help="Только запрос с этим именем.")
    ] = None,
) -> None:
    """Найти вакансии, оценить их и собрать очередь. Ничего не отправляет."""
    config = _load()
    writer = LetterWriter(config)
    provider_list = _providers(config, provider)
    stats = Counter()

    with Storage(DB_PATH) as storage:
        for prov in provider_list:
            specs = config.searches_for(prov.name)
            if search:
                specs = [s for s in specs if s.name == search]
            if not specs:
                continue

            for spec in specs:
                if stats["accepted"] >= max_new:
                    break
                console.print(Rule(f"[bold]{prov.title}[/bold] · {spec.name}"))

                unknown = prov.unknown_params(spec)
                if unknown:
                    console.print(
                        f"[yellow]Незнакомые параметры в '{spec.name}':[/yellow] "
                        f"{', '.join(unknown)} — площадка их проигнорирует."
                    )

                try:
                    for vacancy in prov.search(spec):
                        if stats["accepted"] >= max_new:
                            console.print(
                                f"[yellow]Достигнут лимит --max-new={max_new}.[/yellow]"
                            )
                            break
                        _process_found(
                            vacancy, prov, storage, config, writer, no_letters, stats
                        )
                except AuthError as exc:
                    console.print(f"[red]{exc}[/red]")
                except Exception as exc:  # noqa: BLE001 - не терять собранную очередь
                    console.print(f"[red]Запрос '{spec.name}' прерван:[/red] {exc}")

            if stats["accepted"] >= max_new:
                break

        if writer.llm_disabled_reason:
            console.print(
                f"\n[yellow]LLM недоступна, письма из шаблона:[/yellow] "
                f"{writer.llm_disabled_reason}"
            )

        console.print(
            f"\nПросмотрено: {stats['seen']} · "
            f"обработано ранее: {stats['known']} · "
            f"отсеяно предфильтром: {stats['prefiltered']} · "
            f"отклонено скорингом: {stats['skipped']}"
        )
        console.print(
            f"Готово к отправке ботом: [bold green]{stats['queued']}[/bold green] · "
            f"требуют ручного отклика: [bold]{stats['manual']}[/bold]"
        )

        total_queue = len(storage.queue())
        total_manual = len(storage.queue(status=Status.MANUAL.value))
        console.print(
            f"Всего в очереди: {total_queue} на отправку, {total_manual} вручную."
        )
        hint = "Дальше: [bold]review[/bold] — проверить"
        if total_queue:
            hint += ", [bold]send[/bold] — отправить"
        if total_manual:
            hint += ", [bold]review --manual[/bold] — отклики вручную"
        console.print(hint + ".")


def _process_found(
    vacancy: Vacancy,
    prov: JobProvider,
    storage: Storage,
    config: Config,
    writer: LetterWriter,
    no_letters: bool,
    stats: Counter,
) -> None:
    """Оценить найденную вакансию и положить её в нужную ветку очереди."""
    stats["seen"] += 1

    if storage.is_settled(vacancy.key):
        stats["known"] += 1
        return

    # Первый проход по краткому описанию: отсеиваем явно чужое до запроса
    # за деталями и генерации письма.
    if hard_filter(vacancy, config.scoring):
        stats["prefiltered"] += 1
        return

    vacancy = prov.fetch_details(vacancy)
    score = score_vacancy(vacancy, config.scoring)
    if not score.accepted:
        storage.save(vacancy, score, Status.SKIPPED)
        stats["skipped"] += 1
        return

    letter, source = "", ""
    if not no_letters:
        try:
            letter, source = writer.write(vacancy)
        except LetterError as exc:
            storage.save(vacancy, score, Status.FAILED, detail=f"письмо: {exc}")
            console.print(f"[red]Письмо не создано:[/red] {exc}")
            return

    # Площадка без автоотклика складывает вакансию сразу в «вручную»: письмо
    # готово, но отправить его из бота нельзя, и в очереди на отправку ему
    # делать нечего.
    manual = not prov.can_apply
    storage.save(
        vacancy,
        score,
        Status.MANUAL if manual else Status.QUEUED,
        letter=letter,
        letter_source=source,
    )
    stats["accepted"] += 1
    stats["manual" if manual else "queued"] += 1

    mark = "[cyan]→[/cyan]" if manual else "[green]+[/green]"
    console.print(
        f"{mark}{score.value:>3} {_shorten(vacancy.title, 45)} — "
        f"{_shorten(vacancy.company, 25)}"
    )


@app.command()
def review(
    limit: Annotated[int, typer.Option("--limit", help="Сколько показать.")] = 10,
    full: Annotated[bool, typer.Option("--full", help="Показать письма целиком.")] = False,
    provider: ProviderOption = None,
    manual: Annotated[
        bool,
        typer.Option("--manual", help="Показать вакансии, где отклик нужен вручную."),
    ] = False,
) -> None:
    """Показать очередь: что и с каким письмом будет отправлено."""
    _load()
    status = Status.MANUAL.value if manual else Status.QUEUED.value
    with Storage(DB_PATH) as storage:
        items = storage.queue(
            limit=limit, status=status, providers=[provider] if provider else None
        )
        if not items:
            if manual:
                console.print("[yellow]Нет вакансий, ждущих ручного отклика.[/yellow]")
                return
            waiting = len(storage.queue(status=Status.MANUAL.value))
            console.print("[yellow]Очередь на отправку пуста.[/yellow]")
            if waiting:
                console.print(
                    f"{waiting} вакансий с готовым письмом ждут ручного отклика: "
                    "[bold]review --manual[/bold]"
                )
            else:
                console.print("Запусти [bold]python -m autoapply scan[/bold].")
            return

        title = "Ручной отклик" if manual else "Очередь"
        table = Table(title=f"{title} (показано {len(items)})", expand=False)
        table.add_column("Балл", justify="right", style="green", no_wrap=True)
        table.add_column("Площадка", no_wrap=True)
        table.add_column("Вакансия", min_width=24)
        table.add_column("Компания", min_width=14)
        table.add_column("Зарплата", no_wrap=True)
        table.add_column("Письмо", no_wrap=True)

        for item in items:
            table.add_row(
                str(item.score),
                item.provider,
                _shorten(item.title, 34),
                _shorten(item.company, 18),
                _shorten(item.salary_text or "—", 18),
                item.letter_source or "—",
            )
        console.print(table)

        if manual:
            # Ссылки отдельным списком: в колонке они переносятся и их неудобно
            # копировать, а откликаться предстоит именно по ним.
            console.print("\n[bold]Ссылки для отклика:[/bold]")
            for item in items:
                console.print(f"  [green]{item.score:>3}[/green]  {item.url}")

        if full:
            for item in items:
                console.print(
                    Panel(
                        item.letter or "[письмо не сгенерировано]",
                        title=f"{_shorten(item.title, 45)} — {_shorten(item.company, 25)}",
                        subtitle=item.url,
                        expand=False,
                    )
                )
        else:
            console.print("Письма целиком: [bold]review --full[/bold]")


# --------------------------------------------------------------------------
# Отправка
# --------------------------------------------------------------------------


@app.command()
def send(
    limit: Annotated[
        int | None, typer.Option("--limit", help="Сколько отправить за прогон.")
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Не спрашивать подтверждение.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Показать, что было бы отправлено.")
    ] = False,
    provider: ProviderOption = None,
) -> None:
    """Отправить отклики из очереди."""
    config = _load()
    per_run = limit if limit is not None else config.sending.per_run_limit
    if per_run <= 0:
        _fail(f"Размер порции должен быть больше нуля, получено --limit {per_run}.")

    provider_list = _providers(config, provider)
    senders = {p.name: p for p in provider_list if p.can_apply}
    blocked = [p for p in provider_list if not p.can_apply]

    for prov in blocked:
        console.print(
            f"[yellow]{prov.title}: автоотклик недоступен.[/yellow] "
            f"{prov.authorization_hint()}"
        )
    if not senders:
        console.print(
            "\nНи одна площадка не готова отправлять отклики. Вакансии с готовыми "
            "письмами: [bold]review --manual[/bold]"
        )
        raise typer.Exit(code=1)

    with Storage(DB_PATH) as storage:
        sent_today = storage.sent_today()
        remaining_today = config.sending.daily_limit - sent_today
        if remaining_today <= 0:
            console.print(
                f"[yellow]Суточный лимит исчерпан:[/yellow] отправлено {sent_today} "
                f"из {config.sending.daily_limit}."
            )
            return

        items = storage.queue(
            limit=min(per_run, remaining_today), providers=list(senders)
        )
        if not items:
            waiting = len(storage.queue(status=Status.MANUAL.value))
            if waiting:
                console.print(
                    f"[yellow]Отправлять нечего:[/yellow] в очереди на отправку нет "
                    f"вакансий, а {waiting} ждут ручного отклика — "
                    "[bold]review --manual[/bold]."
                )
            else:
                console.print(
                    "[yellow]Очередь пуста.[/yellow] Запусти "
                    "[bold]python -m autoapply scan[/bold]."
                )
            return

        missing = [i for i in items if not i.letter.strip()]
        if missing:
            _fail(
                f"У {len(missing)} вакансий в очереди нет письма. "
                "Перегенерируй: `scan` без --no-letters."
            )

        console.print(
            f"К отправке: [bold]{len(items)}[/bold] · сегодня уже отправлено "
            f"{sent_today}/{config.sending.daily_limit}"
        )
        for item in items:
            console.print(
                f"  [green]{item.score:>3}[/green] [dim]{item.provider}[/dim] "
                f"{_shorten(item.title, 40)} — {_shorten(item.company, 22)}"
            )

        if dry_run:
            console.print("\n[cyan]--dry-run: ничего не отправлено.[/cyan]")
            return

        if not yes and not typer.confirm(f"\nОтправить {len(items)} откликов?"):
            console.print("Отменено.")
            return

        counters = {"sent": 0, "already": 0, "unavailable": 0, "manual": 0, "failed": 0}
        # Площадка может исчерпать лимит или потерять сессию — тогда она
        # выбывает из прогона, но остальные продолжают работать.
        stopped: set[str] = set()

        for index, item in enumerate(items, start=1):
            if item.provider in stopped:
                continue

            prov = senders[item.provider]
            result = prov.apply(_to_vacancy(item), item.letter)

            if result.limit_reached:
                storage.mark(item.key, Status.QUEUED, result.detail)
                stopped.add(item.provider)
                console.print(f"[yellow]{prov.title} остановлен:[/yellow] {result.detail}")
                if len(stopped) == len(senders):
                    break
                continue

            storage.mark(item.key, result.status, result.detail, result.negotiation_id)

            if result.status is Status.SENT:
                counters["sent"] += 1
                console.print(f"[green]✓[/green] {_shorten(item.title, 50)}")
            elif result.status is Status.ALREADY:
                counters["already"] += 1
                console.print(f"[cyan]•[/cyan] {_shorten(item.title, 45)} — уже был отклик")
            elif result.status is Status.MANUAL:
                counters["manual"] += 1
                console.print(f"[cyan]→[/cyan] {_shorten(item.title, 45)} — вручную")
            elif result.status is Status.UNAVAILABLE:
                counters["unavailable"] += 1
                console.print(
                    f"[yellow]—[/yellow] {_shorten(item.title, 45)} — {result.detail}"
                )
            else:
                counters["failed"] += 1
                console.print(f"[red]✗[/red] {_shorten(item.title, 45)} — {result.detail}")

            if index < len(items):
                time.sleep(config.sending.delay_between)

        console.print(
            f"\nОтправлено: [bold green]{counters['sent']}[/bold green] · "
            f"уже были: {counters['already']} · "
            f"вручную: {counters['manual']} · "
            f"недоступны: {counters['unavailable']} · "
            f"сбоев: {counters['failed']}"
        )


@app.command()
def requeue() -> None:
    """Вернуть в очередь отклики, упавшие по временной причине."""
    _load()
    with Storage(DB_PATH) as storage:
        count = storage.requeue_failed()
    console.print(f"Возвращено в очередь: [bold]{count}[/bold]")


@app.command()
def status() -> None:
    """Статистика по очереди и отправленным откликам."""
    config = _load()
    with Storage(DB_PATH) as storage:
        per_provider = storage.counts_by_provider()
        known = sorted({p for p, _ in per_provider} | set(config.providers))

        table = Table(title="Состояние", expand=False)
        table.add_column("Статус")
        for name in known:
            table.add_column(name, justify="right")
        table.add_column("Всего", justify="right", style="bold")

        for key, label in STATUS_LABELS.items():
            row = [per_provider.get((name, key), 0) for name in known]
            if not any(row):
                continue
            table.add_row(label, *[str(v) for v in row], str(sum(row)))
        console.print(table)

        console.print(
            f"Отправлено сегодня: [bold]{storage.sent_today()}[/bold] "
            f"из лимита {config.sending.daily_limit}"
        )

        recent = storage.recent_sent(limit=10)
        if recent:
            log = Table(title="Последние отклики", expand=False)
            log.add_column("Когда", no_wrap=True)
            log.add_column("Площадка", no_wrap=True)
            log.add_column("Вакансия")
            log.add_column("Компания")
            for item in recent:
                log.add_row(
                    (item.detail or "—")[:16].replace("T", " "),
                    item.provider,
                    _shorten(item.title, 35),
                    _shorten(item.company, 22),
                )
            console.print(log)


def main() -> None:
    app()
