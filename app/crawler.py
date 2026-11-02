"""Выбор игр для очередного захода и upsert в базу.

Правила ТЗ:
1. Первый заход за день: 20 игр из раздела New Releases на странице Games.
2. Следующие заходы: страницы SEE ALL (сортировка «новые»), за заход очередная страница.
3. Новый день начинается заново. Игры, обработанные сегодня, повторно не берём.
"""
from datetime import date, datetime
from typing import Callable

from app.config import settings
from app.metacritic import GameData, MetacriticClient


def today() -> date:
    return datetime.now(settings.tz).date()


def plan_see_all(fetch_page: Callable[[int], list[str]], start_page: int, skip: set[str], need: int,
                 max_pages: int) -> tuple[list[str], int]:
    """Берёт до `need` slug'ов с SEE ALL, начиная со страницы-курсора.

    Страница, с которой забрали всё необработанное, считается пройденной, и курсор уходит дальше.
    Если на странице остались необработанные игры (взяли только часть), курсор остаётся на ней.
    Возвращает (slug'и, новый курсор).
    """
    picked: list[str] = []
    page = start_page
    for _ in range(max_pages):
        if len(picked) >= need:
            break
        slugs = fetch_page(page)
        if not slugs:
            break
        rest = [s for s in dict.fromkeys(slugs) if s not in skip and s not in picked]
        take = rest[: need - len(picked)]
        picked += take
        if len(take) < len(rest):
            break
        page += 1
    return picked, page


def select_batch(conn, client: MetacriticClient, day: date, need: int) -> tuple[list[tuple[str, str]], str]:
    """Выбирает игры для захода и сдвигает состояние дня. Возвращает [(slug, источник)] и описание источника."""
    conn.execute("INSERT INTO crawl_days (day) VALUES (%s) ON CONFLICT DO NOTHING", (day,))
    state = conn.execute("SELECT * FROM crawl_days WHERE day = %s FOR UPDATE", (day,)).fetchone()
    done = {r["slug"] for r in conn.execute("SELECT slug FROM crawl_processed WHERE day = %s", (day,))}
    fetch = lambda p: [it["slug"] for it in client.see_all_page(p)]
    batch: list[tuple[str, str]] = []

    if not state["new_releases_done"]:
        fresh = [s for s in client.new_releases() if s not in done][:need]
        batch = [(s, "new_releases") for s in fresh]
        source = f"New Releases ({len(fresh)})"
        if len(batch) < need:  # в разделе бывает меньше 20 игр: добираем из SEE ALL
            extra, page = plan_see_all(fetch, state["see_all_page"], done | set(fresh), need - len(batch),
                                       settings.max_see_all_pages)
            batch += [(s, "see_all") for s in extra]
            source += f" + SEE ALL стр. {state['see_all_page']}" + (f"-{page - 1}" if page - 1 > state["see_all_page"] else "")
        else:
            page = state["see_all_page"]
        conn.execute("UPDATE crawl_days SET new_releases_done = true, see_all_page = %s, updated_at = now() WHERE day = %s",
                     (page, day))
    else:
        start = state["see_all_page"]
        picked, page = plan_see_all(fetch, start, done, need, settings.max_see_all_pages)
        batch = [(s, "see_all") for s in picked]
        source = f"SEE ALL · новые · стр. {start}" + (f"-{page - 1}" if page - 1 > start else "")
        conn.execute("UPDATE crawl_days SET see_all_page = %s, updated_at = now() WHERE day = %s", (page, day))
    return batch, source


def upsert_game(conn, g: GameData) -> tuple[int, bool]:
    """Идемпотентный upsert по slug. Возвращает (id, была ли игра новой)."""
    row = conn.execute(
        """
        INSERT INTO games (slug, title, release_date, description, genres, developer, publisher, rating, cover_url,
                           video_title, video_url, video_duration, metascore, critic_reviews, critic_sentiment,
                           userscore, user_reviews, user_sentiment, last_crawled_at)
        VALUES (%(slug)s, %(title)s, %(release_date)s, %(description)s, %(genres)s, %(developer)s, %(publisher)s,
                %(rating)s, %(cover_url)s, %(video_title)s, %(video_url)s, %(video_duration)s, %(metascore)s,
                %(critic_reviews)s, %(critic_sentiment)s, %(userscore)s, %(user_reviews)s, %(user_sentiment)s, now())
        ON CONFLICT (slug) DO UPDATE SET
            title = EXCLUDED.title, release_date = EXCLUDED.release_date, description = EXCLUDED.description,
            genres = EXCLUDED.genres, developer = EXCLUDED.developer, publisher = EXCLUDED.publisher,
            rating = EXCLUDED.rating, cover_url = EXCLUDED.cover_url, video_title = EXCLUDED.video_title,
            video_url = EXCLUDED.video_url, video_duration = EXCLUDED.video_duration,
            metascore = EXCLUDED.metascore, critic_reviews = EXCLUDED.critic_reviews,
            critic_sentiment = EXCLUDED.critic_sentiment, userscore = EXCLUDED.userscore,
            user_reviews = EXCLUDED.user_reviews, user_sentiment = EXCLUDED.user_sentiment,
            updated_at = now(), last_crawled_at = now()
        RETURNING id, (xmax = 0) AS inserted
        """,
        {
            "slug": g.slug, "title": g.title, "release_date": g.release_date, "description": g.description,
            "genres": g.genres, "developer": g.developer, "publisher": g.publisher, "rating": g.rating,
            "cover_url": g.cover_url,
            "video_title": (g.video or {}).get("title"), "video_url": (g.video or {}).get("url"),
            "video_duration": (g.video or {}).get("duration"),
            "metascore": g.critic["score"], "critic_reviews": g.critic["reviews"], "critic_sentiment": g.critic["sentiment"],
            "userscore": g.user["score"], "user_reviews": g.user["reviews"], "user_sentiment": g.user["sentiment"],
        },
    ).fetchone()
    game_id = row["id"]
    conn.execute("DELETE FROM game_platforms WHERE game_id = %s", (game_id,))
    for p in g.platforms:
        conn.execute(
            """INSERT INTO game_platforms (game_id, platform_slug, platform, metascore, critic_reviews, userscore, user_reviews)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (game_id, p["platform_slug"], p["platform"], p["metascore"], p["critic_reviews"], p["userscore"], p["user_reviews"]),
        )
    for kind, items in (("critic", g.critic_reviews), ("user", g.user_reviews)):
        for r in items:
            conn.execute(
                """INSERT INTO reviews (game_id, kind, ext_key, source, score, quote, platform, review_date, url)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (game_id, kind, ext_key) DO UPDATE SET score = EXCLUDED.score, fetched_at = now()""",
                (game_id, kind, r["ext_key"], r["source"], r["score"], r["quote"], r["platform"], r["review_date"], r["url"]),
            )
    return game_id, bool(row["inserted"])


def mark_processed(conn, day: date, slug: str, run_id: int, source: str) -> None:
    conn.execute("INSERT INTO crawl_processed (day, slug, run_id, source) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                 (day, slug, run_id, source))
