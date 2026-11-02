"""Очередь запусков, события и статусы воркеров (общие для воркера и API)."""
import json

from psycopg.errors import UniqueViolation

from app.db import pool


def enqueue(trigger: str) -> int | None:
    """Ставит запуск в очередь. None, если запуск уже стоит в очереди или выполняется (защита от двойного старта)."""
    try:
        with pool.connection() as conn:
            row = conn.execute("INSERT INTO runs (trigger, status) VALUES (%s, 'queued') RETURNING id", (trigger,)).fetchone()
            return row["id"]
    except UniqueViolation:
        return None


def claim() -> int | None:
    with pool.connection() as conn:
        row = conn.execute(
            """UPDATE runs SET status = 'running', started_at = now()
               WHERE id = (SELECT id FROM runs WHERE status = 'queued' ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED)
               RETURNING id"""
        ).fetchone()
        return row["id"] if row else None


def active_run() -> dict | None:
    with pool.connection() as conn:
        return conn.execute("SELECT * FROM runs WHERE status IN ('queued', 'running') ORDER BY id LIMIT 1").fetchone()


def emit(run_id: int | None, worker: str, message: str, *, stage: str | None = None, level: str = "info",
         slug: str | None = None, data: dict | None = None) -> None:
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO run_events (run_id, worker, stage, level, slug, message, data) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (run_id, worker, stage, level, slug, message, json.dumps(data, ensure_ascii=False) if data else None),
        )


def bump(run_id: int, **fields) -> None:
    """Атомарно увеличивает счётчики запуска: bump(run_id, games_new=1, cost_usd=0.003)."""
    sets = ", ".join(f"{k} = {k} + %s" for k in fields)
    with pool.connection() as conn:
        conn.execute(f"UPDATE runs SET {sets} WHERE id = %s", (*fields.values(), run_id))


def set_worker(name: str, stage: str, status: str, job: str | None = None, done: int = 0) -> None:
    with pool.connection() as conn:
        conn.execute(
            """INSERT INTO workers (name, stage, status, job, processed, heartbeat_at) VALUES (%s, %s, %s, %s, %s, now())
               ON CONFLICT (name) DO UPDATE SET stage = EXCLUDED.stage, status = EXCLUDED.status, job = EXCLUDED.job,
               processed = workers.processed + %s, heartbeat_at = now()""",
            (name, stage, status, job, done, done),
        )


def heartbeat(names: list[str]) -> None:
    with pool.connection() as conn:
        conn.execute("UPDATE workers SET heartbeat_at = now() WHERE name = ANY(%s)", (names,))
