"""Разовый досчёт: резюме и эмбеддинги для игр, у которых есть отзывы, но нет актуального резюме.

Нужен, если игры прошли конвейер без LLM (например, ключ был не задан). Оформляется обычным запуском,
поэтому виден в мониторинге и не стартует параллельно с плановым заходом.
    docker compose exec games-worker python -m app.backfill
"""
import sys

from psycopg.errors import UniqueViolation

from app import similar
from app.config import settings
from app.db import migrate, open_pool, pool
from app.pipeline import Run
from app.summarizer import Summarizer


def main() -> int:
    if not settings.llm_enabled:
        print("LLM_API_KEY не задан")
        return 1
    open_pool()
    migrate()
    with pool.connection() as conn:
        try:
            run_id = conn.execute(
                "INSERT INTO runs (trigger, status, source, started_at) VALUES ('manual', 'running', 'Досчёт резюме', now()) RETURNING id"
            ).fetchone()["id"]
        except UniqueViolation:
            print("Сейчас идёт другой запуск, попробуйте позже")
            return 1
        games = list(conn.execute(
            """SELECT g.id, g.slug, g.title FROM games g
               WHERE EXISTS (SELECT 1 FROM reviews r WHERE r.game_id = g.id)
                 AND NOT EXISTS (SELECT 1 FROM summaries s WHERE s.game_id = g.id AND s.kind = 'critic')
               ORDER BY g.critic_reviews DESC NULLS LAST"""))
        conn.execute("UPDATE runs SET games_total = %s WHERE id = %s", (len(games), run_id))
    run = Run(run_id)
    run.log("backfill", f"Досчёт резюме: игр с отзывами без резюме {len(games)}", stage="select")
    llm = Summarizer()
    for g in games:
        try:
            run.summarize_game("llm-1", llm, g)
            if settings.embeddings_enabled:
                run.embed_game("matcher-1", g)
        except Exception as e:
            run.fail("llm-1", "summary", g["slug"], e)
    with pool.connection() as conn:
        n = similar.refresh(conn)
        r = conn.execute("UPDATE runs SET status = 'done', finished_at = now(), games_updated = %s WHERE id = %s RETURNING *",
                         (len(games), run_id)).fetchone()
    run.log("backfill", f"Досчёт завершён: {len(games)} игр, ${float(r['cost_usd']):.4f}; похожие пересчитаны для {n} игр",
            stage="done", level="ok")
    print(f"done: {len(games)} games, ${float(r['cost_usd']):.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
