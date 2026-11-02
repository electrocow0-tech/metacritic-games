"""HTTP API и раздача фронтенда (games-web)."""
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from app import crawler
from app.config import settings
from app.db import migrate, open_pool, pool
from app.runs import active_run, emit, enqueue

FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "index.html"
SORTS = {
    "meta": "g.metascore DESC NULLS LAST, g.userscore DESC NULLS LAST",
    "user": "g.userscore DESC NULLS LAST, g.metascore DESC NULLS LAST",
    "date": "g.release_date DESC NULLS LAST, g.metascore DESC NULLS LAST",
    "div": "abs(g.userscore - g.metascore / 10.0) DESC NULLS LAST",
    "title": "lower(g.title)",
}


@asynccontextmanager
async def lifespan(_app):
    open_pool()
    migrate()
    yield


app = FastAPI(title="Scorecast", lifespan=lifespan)


def jsonable(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime):
        return v.isoformat()
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def clean(row: dict) -> dict:
    return {k: jsonable(v) for k, v in row.items()}


# ---------- игры ----------

@app.get("/api/meta")
def meta():
    with pool.connection() as conn:
        g = conn.execute("SELECT count(*) AS total, max(last_crawled_at) AS collected FROM games").fetchone()
        plats = list(conn.execute("SELECT platform AS name, count(*) AS count FROM game_platforms GROUP BY 1 ORDER BY 2 DESC, 1"))
    return {"total": g["total"], "collectedAt": jsonable(g["collected"]), "platforms": plats, "tz": settings.tz_name}


@app.get("/api/games")
def list_games(q: str = "", platform: list[str] = Query(default=[]), sort: str = "meta",
               limit: int = Query(60, le=200), offset: int = 0):
    where, params = ["true"], {}
    if q.strip():
        where.append("g.title ILIKE %(q)s")
        params["q"] = f"%{q.strip()}%"
    if platform:
        where.append("EXISTS (SELECT 1 FROM game_platforms p WHERE p.game_id = g.id AND p.platform = ANY(%(plats)s))")
        params["plats"] = platform
    order = SORTS.get(sort, SORTS["meta"])
    sql_where = " AND ".join(where)
    with pool.connection() as conn:
        total = conn.execute(f"SELECT count(*) AS n FROM games g WHERE {sql_where}", params).fetchone()["n"]
        rows = conn.execute(
            f"""SELECT g.slug, g.title, g.release_date AS "releaseDate", g.developer, g.genres, g.metascore, g.userscore,
                       coalesce(g.tagline, left(g.description, 110)) AS tagline, g.cover_url AS "coverUrl",
                       g.last_crawled_at AS "lastCrawledAt", g.first_seen_at AS "firstSeenAt",
                       coalesce((SELECT json_agg(json_build_object('name', p.platform, 'metascore', p.metascore,
                                 'userscore', p.userscore) ORDER BY p.metascore DESC NULLS LAST, p.platform)
                                 FROM game_platforms p WHERE p.game_id = g.id), '[]') AS platforms
                FROM games g WHERE {sql_where} ORDER BY {order}, g.id LIMIT %(limit)s OFFSET %(offset)s""",
            {**params, "limit": limit, "offset": offset},
        ).fetchall()
    return {"total": total, "items": [clean(r) for r in rows]}


@app.get("/api/games/{slug}")
def game(slug: str):
    with pool.connection() as conn:
        g = conn.execute("SELECT * FROM games WHERE slug = %s", (slug,)).fetchone()
        if not g:
            raise HTTPException(404, "game not found")
        plats = list(conn.execute(
            """SELECT platform AS name, metascore, critic_reviews AS "criticReviews", userscore, user_reviews AS "userReviews"
               FROM game_platforms WHERE game_id = %s ORDER BY metascore DESC NULLS LAST, platform""", (g["id"],)))
        sums = {r["kind"]: r for r in conn.execute("SELECT * FROM summaries WHERE game_id = %s", (g["id"],))}
        counts = {r["kind"]: r["n"] for r in conn.execute(
            "SELECT kind, count(*) AS n FROM reviews WHERE game_id = %s GROUP BY kind", (g["id"],))}
        sim = list(conn.execute(
            """SELECT o.slug, o.title, o.metascore, o.userscore, o.cover_url AS "coverUrl", s.score, s.reasons
               FROM similar_games s JOIN games o ON o.id = s.similar_id WHERE s.game_id = %s ORDER BY s.rank""", (g["id"],)))
        lp = conn.execute("SELECT * FROM letsplays WHERE game_id = %s", (g["id"],)).fetchone()

    def summary(kind):
        s = sums.get(kind)
        if not s:
            return None
        return {"verdict": s["verdict"], "likes": s["likes"], "dislikes": s["dislikes"], "reviewsUsed": s["reviews_used"],
                "model": s["model"], "updatedAt": jsonable(s["updated_at"])}

    return {
        "slug": g["slug"], "title": g["title"], "releaseDate": jsonable(g["release_date"]), "description": g["description"],
        "genres": g["genres"], "developer": g["developer"], "publisher": g["publisher"], "rating": g["rating"],
        "coverUrl": g["cover_url"], "tagline": g["tagline"],
        "metascore": g["metascore"], "userscore": jsonable(g["userscore"]),
        "criticSentiment": g["critic_sentiment"], "userSentiment": g["user_sentiment"],
        "criticTotal": g["critic_reviews"], "userTotal": g["user_reviews"],
        "analyzed": {"critic": counts.get("critic", 0), "user": counts.get("user", 0)},
        "platforms": [clean(p) for p in plats],
        "video": {"title": g["video_title"], "embedUrl": g["video_url"], "duration": g["video_duration"]} if g["video_url"] else None,
        "summary": {"critics": summary("critic"), "players": summary("user")},
        "similar": [clean(s) for s in sim],
        "letsplay": {"videoId": lp["video_id"], "url": f"https://www.youtube.com/watch?v={lp['video_id']}", "title": lp["title"],
                     "channel": lp["channel"], "views": lp["views"], "durationSec": lp["duration_sec"], "summary": lp["summary"],
                     "moments": lp["moments"], "updatedAt": jsonable(lp["updated_at"])} if lp else None,
        "firstSeenAt": jsonable(g["first_seen_at"]), "lastCrawledAt": jsonable(g["last_crawled_at"]),
        "source": f"https://www.metacritic.com/game/{g['slug']}/",
    }


# ---------- мониторинг ----------

def next_run_at() -> datetime:
    now = datetime.now(settings.tz)
    nxt = now.replace(minute=settings.crawl_cron_minute, second=0, microsecond=0)
    return nxt if nxt > now else nxt + timedelta(hours=1)


def monitor_state() -> dict:
    day = crawler.today()
    with pool.connection() as conn:
        run = conn.execute(
            "SELECT * FROM runs WHERE status IN ('queued', 'running') ORDER BY id LIMIT 1").fetchone() or conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        stages = {}
        if run:
            for r in conn.execute(
                    """SELECT stage, count(DISTINCT slug) AS n FROM run_events
                       WHERE run_id = %s AND slug IS NOT NULL AND level <> 'error' GROUP BY stage""", (run["id"],)):
                stages[r["stage"]] = r["n"]
            costs = list(conn.execute(
                """SELECT slug, max(data->>'title') AS title, sum((data->>'tokens_in')::int) AS tokens_in,
                          sum(coalesce((data->>'tokens_out')::int, 0)) AS tokens_out, sum((data->>'cost')::numeric) AS cost
                   FROM run_events WHERE run_id = %s AND data ? 'cost' GROUP BY slug ORDER BY max(id) DESC LIMIT 8""",
                (run["id"],)))
        else:
            costs = []
        workers = list(conn.execute(
            """SELECT name, stage, status, job, processed, heartbeat_at,
                      (now() - heartbeat_at) > interval '30 seconds' AS stale FROM workers ORDER BY name"""))
        hist = list(conn.execute(
            "SELECT id, status, games_total, errors, trigger, started_at FROM runs WHERE status <> 'queued' ORDER BY id DESC LIMIT 12"))
        totals = conn.execute(
            """SELECT (SELECT count(*) FROM games) AS games,
                      (SELECT count(*) FROM crawl_processed WHERE day = %(d)s) AS today,
                      (SELECT count(*) FROM reviews) AS reviews,
                      (SELECT coalesce(sum(errors), 0) FROM runs WHERE created_at::date = %(d)s) AS errors_today""",
            {"d": day}).fetchone()
        crawl = conn.execute("SELECT * FROM crawl_days WHERE day = %s", (day,)).fetchone()
    games_done = (run or {}).get("games_new", 0) + (run or {}).get("games_updated", 0)
    return {
        "now": datetime.now(settings.tz).isoformat(),
        "nextRunAt": next_run_at().isoformat(),
        "run": clean(run) if run else None,
        "stages": stages,
        "counters": {**{k: jsonable(v) for k, v in totals.items()},
                     "tokensRun": (run["tokens_in"] + run["tokens_out"]) if run else 0,
                     "costPerGame": float(run["cost_usd"]) / games_done if run and games_done else None},
        "workers": [clean(w) for w in workers],
        "costs": [clean(c) for c in costs],
        "history": [clean(h) for h in reversed(hist)],
        "crawl": {"day": day.isoformat(), "newReleasesDone": bool(crawl and crawl["new_releases_done"]),
                  "seeAllPage": crawl["see_all_page"] if crawl else 1, "processedToday": totals["today"]},
        "llm": {"enabled": settings.llm_enabled, "model": settings.llm_model,
                "priceIn": settings.llm_price_in, "priceOut": settings.llm_price_out},
    }


def events_after(after: int, limit: int = 200) -> list[dict]:
    with pool.connection() as conn:
        if after <= 0:
            rows = list(conn.execute("SELECT * FROM (SELECT * FROM run_events ORDER BY id DESC LIMIT %s) t ORDER BY id", (limit,)))
        else:
            rows = list(conn.execute("SELECT * FROM run_events WHERE id > %s ORDER BY id LIMIT %s", (after, limit)))
    return [clean(r) for r in rows]


@app.get("/api/monitor/state")
def get_state():
    return monitor_state()


@app.get("/api/monitor/events")
def get_events(after: int = 0, limit: int = Query(100, le=500)):
    return events_after(after, limit)


@app.get("/api/monitor/stream")
async def stream(after: int = 0):
    """SSE: раз в секунду состояние конвейера и новые события лога."""
    async def gen():
        last = after
        while True:
            events = await run_in_threadpool(events_after, last)
            if events:
                last = events[-1]["id"]
                yield f"event: log\ndata: {json.dumps(events, ensure_ascii=False)}\n\n"
            state = await run_in_threadpool(monitor_state)
            yield f"event: state\ndata: {json.dumps(state, ensure_ascii=False, default=str)}\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/runs", status_code=202)
def start_run():
    run_id = enqueue("manual")
    if run_id is None:
        emit(None, "api", "POST /api/runs → 409: запуск уже выполняется", level="warn")
        return JSONResponse({"detail": "run already in progress", "run": clean(active_run() or {})}, status_code=409)
    emit(run_id, "api", f"Ручной запуск #{run_id} поставлен в очередь")
    return {"id": run_id, "status": "queued"}


@app.get("/health")
def health():
    with pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(FRONTEND, headers={"Cache-Control": "no-cache"})
