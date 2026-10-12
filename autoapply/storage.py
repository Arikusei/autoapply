"""Состояние бота в SQLite: что видели, что в очереди, что отправили.

Хранилище — единственный источник правды о том, на какие вакансии мы уже
откликались. Благодаря этому повторные прогоны не дублируют отклики.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from .models import Score, Status, Vacancy

SCHEMA = """
CREATE TABLE IF NOT EXISTS vacancies (
    key            TEXT PRIMARY KEY,
    provider       TEXT NOT NULL,
    external_id    TEXT NOT NULL,
    title          TEXT NOT NULL,
    company        TEXT NOT NULL,
    url            TEXT NOT NULL,
    area_name      TEXT,
    salary_text    TEXT,
    experience     TEXT,
    schedule       TEXT,
    published_at   TEXT,
    score          INTEGER,
    score_json     TEXT,
    status         TEXT NOT NULL,
    letter         TEXT,
    letter_source  TEXT,
    detail         TEXT,
    negotiation_id TEXT,
    first_seen     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    sent_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_vacancies_status ON vacancies(status);
CREATE INDEX IF NOT EXISTS idx_vacancies_sent_at ON vacancies(sent_at);
"""


@dataclass
class QueueItem:
    """Строка очереди, готовая к показу или отправке."""

    key: str
    provider: str
    external_id: str
    title: str
    company: str
    url: str
    area_name: str | None
    salary_text: str | None
    score: int
    status: str
    letter: str
    letter_source: str | None
    detail: str | None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _to_item(row: sqlite3.Row, detail_field: str = "detail") -> QueueItem:
    return QueueItem(
        key=row["key"],
        provider=row["provider"],
        external_id=row["external_id"],
        title=row["title"],
        company=row["company"],
        url=row["url"],
        area_name=row["area_name"],
        salary_text=row["salary_text"],
        score=row["score"] or 0,
        status=row["status"],
        letter=row["letter"] or "",
        letter_source=row["letter_source"],
        detail=row[detail_field],
    )


class Storage:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        with closing(self.conn.cursor()) as cur:
            cur.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- чтение ---------------------------------------------------------

    def known_status(self, key: str) -> str | None:
        """Статус вакансии, если мы её уже видели."""
        row = self.conn.execute(
            "SELECT status FROM vacancies WHERE key = ?", (key,)
        ).fetchone()
        return row["status"] if row else None

    def is_settled(self, key: str) -> bool:
        """True, если по вакансии уже принято окончательное решение.

        Такие вакансии повторно не скорим и не переписываем письмо: отклик уже
        отправлен, вакансия недоступна, ждёт ручного отклика или отброшена.
        """
        status = self.known_status(key)
        return status in {
            Status.SENT.value,
            Status.ALREADY.value,
            Status.UNAVAILABLE.value,
            Status.SKIPPED.value,
            Status.MANUAL.value,
        }

    def queue(
        self,
        limit: int | None = None,
        status: str = Status.QUEUED.value,
        providers: list[str] | None = None,
    ) -> list[QueueItem]:
        sql = "SELECT * FROM vacancies WHERE status = ?"
        params: list = [status]
        if providers is not None:
            if not providers:
                return []
            placeholders = ",".join("?" * len(providers))
            sql += f" AND provider IN ({placeholders})"
            params.extend(providers)
        sql += " ORDER BY score DESC, first_seen ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)

        rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [_to_item(r) for r in rows]

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM vacancies GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def sent_today(self) -> int:
        today = date.today().isoformat()
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM vacancies "
            "WHERE status = ? AND sent_at IS NOT NULL AND substr(sent_at, 1, 10) = ?",
            (Status.SENT.value, today),
        ).fetchone()
        return row["n"] if row else 0

    def recent_sent(self, limit: int = 20) -> list[QueueItem]:
        rows = self.conn.execute(
            "SELECT * FROM vacancies WHERE status = ? ORDER BY sent_at DESC LIMIT ?",
            (Status.SENT.value, limit),
        ).fetchall()
        # В detail кладём время отправки — так его показывает `status`.
        return [_to_item(r, detail_field="sent_at") for r in rows]

    def counts_by_provider(self) -> dict[tuple[str, str], int]:
        rows = self.conn.execute(
            "SELECT provider, status, COUNT(*) AS n FROM vacancies "
            "GROUP BY provider, status"
        ).fetchall()
        return {(r["provider"], r["status"]): r["n"] for r in rows}

    # --- запись ---------------------------------------------------------

    def save(
        self,
        vacancy: Vacancy,
        score: Score,
        status: Status,
        letter: str = "",
        letter_source: str = "",
        detail: str = "",
    ) -> None:
        now = _now()
        self.conn.execute(
            """
            INSERT INTO vacancies (
                key, provider, external_id, title, company, url, area_name,
                salary_text, experience, schedule, published_at, score, score_json,
                status, letter, letter_source, detail, first_seen, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET
                title=excluded.title,
                company=excluded.company,
                url=excluded.url,
                score=excluded.score,
                score_json=excluded.score_json,
                status=excluded.status,
                letter=excluded.letter,
                letter_source=excluded.letter_source,
                detail=excluded.detail,
                updated_at=excluded.updated_at
            """,
            (
                vacancy.key,
                vacancy.provider,
                vacancy.external_id,
                vacancy.title,
                vacancy.company,
                vacancy.url,
                vacancy.area_name,
                vacancy.salary_text,
                vacancy.experience,
                vacancy.schedule,
                vacancy.published_at.isoformat() if vacancy.published_at else None,
                score.value,
                score.to_json(),
                status.value,
                letter,
                letter_source,
                detail,
                now,
                now,
            ),
        )
        self.conn.commit()

    def mark(
        self,
        key: str,
        status: Status,
        detail: str = "",
        negotiation_id: str | None = None,
    ) -> None:
        now = _now()
        sent_at = now if status is Status.SENT else None
        self.conn.execute(
            "UPDATE vacancies SET status = ?, detail = ?, negotiation_id = ?, "
            "updated_at = ?, sent_at = COALESCE(?, sent_at) WHERE key = ?",
            (status.value, detail, negotiation_id, now, sent_at, key),
        )
        self.conn.commit()

    def requeue_failed(self) -> int:
        """Вернуть в очередь отклики, упавшие по временной причине."""
        cur = self.conn.execute(
            "UPDATE vacancies SET status = ?, updated_at = ? WHERE status = ?",
            (Status.QUEUED.value, _now(), Status.FAILED.value),
        )
        self.conn.commit()
        return cur.rowcount
