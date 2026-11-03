"""Разовый досчёт по всей базе, оформленный обычным запуском (виден в мониторинге, не стартует параллельно с обходом).

1. Игры без сохранённых отзывов перезапрашиваются с Metacritic: оценки, платформы и отзывы могли появиться позже.
2. Резюме строятся или обновляются там, где изменился набор отзывов; эмбеддинги там, где изменился текст.
3. Летсплеи: ищутся для игр без ссылки, а сохранённые ссылки перепроверяются на релевантность.
4. Похожие пересчитываются для всей базы.

    docker compose exec games-worker python -m app.backfill [--skip-letsplay | --only-letsplay]
"""
import sys

from psycopg.errors import UniqueViolation

from app import crawler, letsplay, similar
from app.config import settings
from app.db import migrate, open_pool, pool
from app.metacritic import MetacriticClient
from app.pipeline import Run
from app.runs import set_worker
from app.summarizer import Summarizer


def start_run(source: str) -> int | None:
    with pool.connection() as conn:
        try:
            return conn.execute(
                """INSERT INTO runs (trigger, status, source, started_at)
                   VALUES ('manual', 'running', %s, now()) RETURNING id""", (source,)
            ).fetchone()["id"]
        except UniqueViolation:
            return None


def main(skip_letsplay: bool = False, only_letsplay: bool = False) -> int:
    open_pool()
    migrate()
    run_id = start_run("Досчёт летсплеев" if only_letsplay else "Досчёт: отзывы, резюме, летсплеи")
    if run_id is None:
        print("Сейчас идёт другой запуск, попробуйте позже")
        return 1
    run = Run(run_id)
    try:
        with pool.connection() as conn:
            games = list(conn.execute("SELECT id, slug, title FROM games ORDER BY id"))
            no_reviews = [g for g in games if not conn.execute(
                "SELECT 1 FROM reviews WHERE game_id = %s LIMIT 1", (g["id"],)).fetchone()]
            conn.execute("UPDATE runs SET games_total = %s WHERE id = %s", (len(games), run_id))
        if only_letsplay:
            no_reviews = []
        run.log("backfill", f"Игр в базе {len(games)}, без отзывов {len(no_reviews)}: перезапрашиваю их с Metacritic",
                stage="select")

        # 1. догрузка отзывов
        client = MetacriticClient()
        got = 0
        for g in no_reviews:
            set_worker("collector-1", "collect", "busy", g["title"])
            try:
                data = client.game(g["slug"])
                with pool.connection() as conn, conn.transaction():
                    crawler.upsert_game(conn, data)
                n = len(data.critic_reviews) + len(data.user_reviews)
                if n:
                    got += 1
                    run.log("collector-1", f"«{g['title']}»: появились отзывы ({len(data.critic_reviews)} рецензий, "
                                           f"{len(data.user_reviews)} отзывов игроков)", stage="parse", slug=g["slug"])
            except Exception as e:
                run.fail("collector-1", "collect", g["slug"], e)
            set_worker("collector-1", "collect", "idle", None, done=1)
        client.close()
        run.log("backfill", f"Отзывы появились у {got} из {len(no_reviews)} игр", stage="parse")

        # 2. резюме и эмбеддинги (сами пропускают то, что не изменилось)
        if not only_letsplay:
            llm = Summarizer() if settings.llm_enabled else None
            with pool.connection() as conn:
                with_reviews = list(conn.execute(
                    "SELECT id, slug, title FROM games g WHERE EXISTS (SELECT 1 FROM reviews r WHERE r.game_id = g.id) ORDER BY id"))
            for g in with_reviews:
                set_worker("llm-1", "summary", "busy", g["title"])
                try:
                    run.summarize_game("llm-1", llm, g)
                except Exception as e:
                    run.fail("llm-1", "summary", g["slug"], e)
                set_worker("llm-1", "summary", "idle", None, done=1)
            if settings.embeddings_enabled:
                for g in games:
                    try:
                        run.embed_game("matcher-1", g)
                    except Exception as e:
                        run.fail("matcher-1", "embed", g["slug"], e)

        # 3. летсплеи: нет ссылки, ссылка не про эту игру или нет заключения
        if not skip_letsplay:
            with pool.connection() as conn:
                lps = {r["game_id"]: r for r in conn.execute("SELECT game_id, title, summary FROM letsplays")}
            irrelevant = [g for g in games if g["id"] in lps and not letsplay.is_relevant(g["title"], lps[g["id"]]["title"])]
            no_summary = [g for g in games if g["id"] in lps and g not in irrelevant and not lps[g["id"]]["summary"]]
            missing = [g for g in games if g["id"] not in lps]
            run.log("backfill", f"Летсплеи: без заключения {len(no_summary)}, без ссылки {len(missing)}, "
                                f"нерелевантных {len(irrelevant)}", stage="letsplay")
            for g in no_summary + irrelevant + missing:
                set_worker("letsplay-1", "letsplay", "busy", g["title"])
                try:
                    # для релевантной ссылки ролик заново не ищем, сразу берём субтитры
                    letsplay.process(run, g, force=True, reuse_video=True)
                except Exception as e:
                    run.fail("letsplay-1", "letsplay", g["slug"], e)
                set_worker("letsplay-1", "letsplay", "idle", None, done=1)

        # 4. похожие
        with pool.connection() as conn:
            n = similar.refresh(conn)
            r = conn.execute("UPDATE runs SET status = 'done', finished_at = now(), games_updated = %s WHERE id = %s RETURNING *",
                             (len(no_reviews), run_id)).fetchone()
        run.log("backfill", f"Досчёт завершён: ошибок {r['errors']}, ${float(r['cost_usd']):.4f}; похожие пересчитаны для {n} игр",
                stage="done", level="ok")
        print(f"done: games {len(games)}, refetched {len(no_reviews)}, got reviews {got}, errors {r['errors']}, ${float(r['cost_usd']):.4f}")
        return 0
    except Exception as e:
        with pool.connection() as conn:
            conn.execute("UPDATE runs SET status = 'failed', finished_at = now(), error = %s WHERE id = %s", (str(e), run_id))
        raise


if __name__ == "__main__":
    sys.exit(main(skip_letsplay="--skip-letsplay" in sys.argv, only_letsplay="--only-letsplay" in sys.argv))
