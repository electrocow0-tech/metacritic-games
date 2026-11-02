"""Процесс воркера: планировщик раз в час + выполнение запусков из очереди (плановых и ручных)."""
import logging
import threading
import time

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import settings
from app.db import open_pool, migrate, pool
from app.pipeline import execute_run
from app.runs import claim, emit, enqueue, heartbeat

log = logging.getLogger("worker")
WORKERS = ["collector-1", "parser-1", *[f"llm-{n}" for n in range(1, settings.llm_workers + 1)], "matcher-1", "letsplay-1"]
STAGES = {"collector-1": "collect", "parser-1": "parse", "matcher-1": "similar", "letsplay-1": "letsplay"}


def scheduled_run():
    run_id = enqueue("scheduled")
    if run_id is None:
        emit(None, "scheduler", "Плановый запуск пропущен: предыдущий ещё выполняется", level="warn")
    else:
        emit(run_id, "scheduler", f"Плановый запуск #{run_id} поставлен в очередь")


def init_workers():
    with pool.connection() as conn:
        # после рестарта процесса незавершённые запуски помечаем упавшими, иначе очередь заблокирована
        conn.execute("UPDATE runs SET status = 'failed', finished_at = now(), error = 'worker restarted' WHERE status = 'running'")
        conn.execute("DELETE FROM workers WHERE NOT (name = ANY(%s))", (WORKERS,))
        for w in WORKERS:
            conn.execute(
                """INSERT INTO workers (name, stage, status) VALUES (%s, %s, 'idle')
                   ON CONFLICT (name) DO UPDATE SET status = 'idle', job = NULL, heartbeat_at = now()""",
                (w, STAGES.get(w, "summary")),
            )
        empty = conn.execute("SELECT count(*) AS n FROM games").fetchone()["n"] == 0
    if empty:  # пустая база после деплоя: не ждём начала часа
        enqueue("scheduled")


def heartbeat_loop():
    while True:
        try:
            heartbeat(WORKERS)
        except Exception:
            log.exception("heartbeat failed")
        time.sleep(5)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    open_pool()
    migrate()
    init_workers()
    sched = BackgroundScheduler(timezone=settings.tz)
    sched.add_job(scheduled_run, "cron", minute=settings.crawl_cron_minute, id="hourly", max_instances=1, coalesce=True)
    sched.start()
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    log.info("worker started, schedule: every hour at minute %s (%s)", settings.crawl_cron_minute, settings.tz_name)
    while True:
        run_id = claim()
        if run_id is None:
            time.sleep(2)
            continue
        log.info("run %s started", run_id)
        execute_run(run_id)
        log.info("run %s finished", run_id)


if __name__ == "__main__":
    main()
