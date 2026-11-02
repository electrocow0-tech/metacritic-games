"""Пул соединений PostgreSQL и простые миграции (SQL-файлы по порядку)."""
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import settings

MIGRATIONS = Path(__file__).parent / "migrations"

pool = ConnectionPool(settings.dsn, min_size=1, max_size=10, open=False,
                      kwargs={"row_factory": dict_row, "autocommit": True,
                              "options": f"-c timezone={settings.tz_name}"})


def open_pool() -> None:
    pool.open(wait=True)


def migrate() -> None:
    with pool.connection() as conn:
        conn.execute("SELECT pg_advisory_lock(424242)")  # API и воркер стартуют одновременно
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz DEFAULT now())")
        done = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations")}
        for f in sorted(MIGRATIONS.glob("*.sql")):
            if f.name in done:
                continue
            with conn.transaction():
                conn.execute(f.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (f.name,))
        conn.execute("SELECT pg_advisory_unlock(424242)")
