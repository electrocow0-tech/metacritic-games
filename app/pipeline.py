"""Конвейер одного захода: сбор → разбор (upsert) → резюме → эмбеддинг и похожие → летсплей.

Воркеры связаны очередями: сборщик один (вежливая частота запросов), LLM-воркеров несколько.
Каждый шаг пишет событие в run_events, по ним работает мониторинг.
"""
import json
import queue
import threading
import traceback

from app import crawler, similar
from app.config import settings
from app.db import pool
from app.metacritic import MetacriticClient
from app.runs import bump, emit, set_worker
from app.summarizer import Summarizer, reviews_hash

STOP = object()


class Run:
    def __init__(self, run_id: int):
        self.id = run_id
        self.day = crawler.today()
        self.q_summary: queue.Queue = queue.Queue()
        self.q_match: queue.Queue = queue.Queue()
        self.q_letsplay: queue.Queue = queue.Queue()
        self.llm_warned = False
        self.lock = threading.Lock()

    def log(self, worker, message, **kw):
        emit(self.id, worker, message, **kw)

    def fail(self, worker, stage, slug, exc):
        self.log(worker, f"Ошибка: {exc}", stage=stage, level="error", slug=slug,
                 data={"trace": traceback.format_exc(limit=3)})
        bump(self.id, errors=1)

    # ---------- воркеры ----------

    def collector(self, batch: list[tuple[str, str]]):
        name = "collector-1"
        client = MetacriticClient(on_retry=lambda url, st, wait: self.log(
            name, f"HTTP {st}, повтор через {wait} с", stage="collect", level="warn"))
        try:
            for slug, source in batch:
                set_worker(name, "collect", "busy", slug)
                try:
                    g = client.game(slug)
                    set_worker("parser-1", "parse", "busy", g.title)
                    with pool.connection() as conn, conn.transaction():
                        game_id, is_new = crawler.upsert_game(conn, g)
                        crawler.mark_processed(conn, self.day, slug, self.id, source)
                    set_worker("parser-1", "parse", "idle", None, done=1)
                    bump(self.id, **({"games_new": 1} if is_new else {"games_updated": 1}))
                    self.log(name, f"«{g.title}»: {'новая игра' if is_new else 'обновление'}, платформ {len(g.platforms)}, "
                                   f"рецензий {len(g.critic_reviews)}, отзывов игроков {len(g.user_reviews)}",
                             stage="parse", slug=slug)
                    self.q_summary.put({"id": game_id, "slug": slug, "title": g.title})
                    set_worker(name, "collect", "idle", None, done=1)
                except Exception as e:  # одна игра не должна ронять весь заход
                    set_worker(name, "collect", "idle")
                    self.fail(name, "collect", slug, e)
        finally:
            client.close()
            for _ in range(settings.llm_workers):
                self.q_summary.put(STOP)

    def summarizer(self, n: int):
        name = f"llm-{n}"
        llm = Summarizer() if settings.llm_enabled else None
        while (item := self.q_summary.get()) is not STOP:
            set_worker(name, "summary", "busy", item["title"])
            try:
                self.summarize_game(name, llm, item)
            except Exception as e:
                self.fail(name, "summary", item["slug"], e)
            set_worker(name, "summary", "idle", None, done=1)
            self.q_match.put(item)
        self.q_match.put(STOP)

    def summarize_game(self, name, llm, item):
        with pool.connection() as conn:
            revs = {k: list(conn.execute(
                "SELECT ext_key, source, score, quote FROM reviews WHERE game_id = %s AND kind = %s ORDER BY review_date DESC NULLS LAST, id",
                (item["id"], k))) for k in ("critic", "user")}
            old = {r["kind"]: r["reviews_hash"] for r in conn.execute(
                "SELECT kind, reviews_hash FROM summaries WHERE game_id = %s", (item["id"],))}
            totals = conn.execute("SELECT critic_reviews, user_reviews FROM games WHERE id = %s", (item["id"],)).fetchone()
        for kind, reviews in revs.items():
            h = reviews_hash(reviews)
            if old.get(kind) == h:
                self.log(name, f"«{item['title']}»: отзывы {kind} не изменились, резюме актуально", stage="summary", slug=item["slug"])
                continue
            if llm is None:
                with self.lock:
                    first, self.llm_warned = not self.llm_warned, True
                self.log(name, f"«{item['title']}»: резюме пропущено, LLM_API_KEY не задан", stage="summary",
                         level="warn" if first else "info", slug=item["slug"])
                return
            s = llm.summarize(item["title"], kind, reviews)
            if s is None:
                self.log(name, f"«{item['title']}»: {'рецензий' if kind == 'critic' else 'отзывов игроков'} "
                               f"{len(reviews)}, мало для резюме", stage="summary", slug=item["slug"])
                continue
            with pool.connection() as conn, conn.transaction():
                conn.execute(
                    """INSERT INTO summaries (game_id, kind, verdict, likes, dislikes, reviews_used, reviews_total, reviews_hash,
                                              model, tokens_in, tokens_out, cost_usd, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                       ON CONFLICT (game_id, kind) DO UPDATE SET verdict = EXCLUDED.verdict, likes = EXCLUDED.likes,
                         dislikes = EXCLUDED.dislikes, reviews_used = EXCLUDED.reviews_used, reviews_total = EXCLUDED.reviews_total,
                         reviews_hash = EXCLUDED.reviews_hash, model = EXCLUDED.model, tokens_in = EXCLUDED.tokens_in,
                         tokens_out = EXCLUDED.tokens_out, cost_usd = EXCLUDED.cost_usd, updated_at = now()""",
                    (item["id"], kind, s.verdict, json.dumps(s.likes, ensure_ascii=False), json.dumps(s.dislikes, ensure_ascii=False),
                     s.reviews_used, totals[f"{kind}_reviews"], h, settings.llm_model, s.tokens_in, s.tokens_out, s.cost_usd),
                )
                if kind == "critic" and s.tagline:
                    conn.execute("UPDATE games SET tagline = %s WHERE id = %s", (s.tagline, item["id"]))
            bump(self.id, tokens_in=s.tokens_in, tokens_out=s.tokens_out, cost_usd=s.cost_usd)
            self.log(name, f"«{item['title']}»: резюме {'критиков' if kind == 'critic' else 'игроков'}, "
                           f"{s.tokens_in} → {s.tokens_out} токенов, ${s.cost_usd:.4f}"
                           + (f", отброшено тезисов без цитаты: {s.dropped}" if s.dropped else ""),
                     stage="summary", slug=item["slug"],
                     data={"tokens_in": s.tokens_in, "tokens_out": s.tokens_out, "cost": s.cost_usd, "title": item["title"]})

    def matcher(self):
        name = "matcher-1"
        stops = 0
        while True:
            item = self.q_match.get()
            if item is STOP:
                stops += 1
                if stops == settings.llm_workers:
                    break
                continue
            set_worker(name, "similar", "busy", item["title"])
            try:
                if settings.embeddings_enabled:
                    try:
                        self.embed_game(name, item)
                    except Exception as e:  # без эмбеддинга похожие всё равно считаются по признакам
                        self.log(name, f"«{item['title']}»: эмбеддинг не получен ({e}), похожие по признакам",
                                 stage="embed", level="warn", slug=item["slug"])
                else:
                    self.log(name, f"«{item['title']}»: вектор признаков (жанры, компании, платформы, TF-IDF описания)",
                             stage="embed", slug=item["slug"])
                with pool.connection() as conn:
                    similar.refresh(conn, [item["id"]])
                    top = list(conn.execute(
                        "SELECT g.title FROM similar_games s JOIN games g ON g.id = s.similar_id WHERE s.game_id = %s ORDER BY rank LIMIT 3",
                        (item["id"],)))
                self.log(name, f"«{item['title']}»: похожие {', '.join(r['title'] for r in top) or 'пока не найдены'}",
                         stage="similar", slug=item["slug"])
            except Exception as e:
                self.fail(name, "similar", item["slug"], e)
            set_worker(name, "similar", "idle", None, done=1)
            self.q_letsplay.put(item)
        self.q_letsplay.put(STOP)

    def embed_game(self, name, item):
        with pool.connection() as conn:
            g = conn.execute("SELECT id, title, genres, description, embedding_hash, embedding_model FROM games WHERE id = %s",
                             (item["id"],)).fetchone()
            verdict = conn.execute("SELECT verdict FROM summaries WHERE game_id = %s AND kind = 'critic'", (item["id"],)).fetchone()
        text = similar.embed_text(g, verdict and verdict["verdict"])
        h = reviews_hash([{"ext_key": text}]) + settings.embedding_model
        if g["embedding_hash"] == h:
            return
        vec, tokens = similar.embed(text)
        cost = tokens * settings.embedding_price / 1e6
        with pool.connection() as conn:
            conn.execute("UPDATE games SET embedding = %s, embedding_model = %s, embedding_hash = %s WHERE id = %s",
                         (vec, settings.embedding_model, h, item["id"]))
        bump(self.id, tokens_in=tokens, cost_usd=cost)
        self.log(name, f"«{item['title']}»: эмбеддинг {len(vec)}d, {tokens} токенов", stage="embed", slug=item["slug"],
                 data={"tokens_in": tokens, "cost": cost, "title": item["title"]})

    def letsplayer(self):
        """Летсплеи (доп. часть 1). Подключается модулем app.letsplay, если он доступен."""
        try:
            from app import letsplay
        except ImportError:
            letsplay = None
        while (item := self.q_letsplay.get()) is not STOP:
            if letsplay is None or not settings.letsplay_enabled:
                continue
            set_worker("letsplay-1", "letsplay", "busy", item["title"])
            try:
                letsplay.process(self, item)
            except Exception as e:
                self.fail("letsplay-1", "letsplay", item["slug"], e)
            set_worker("letsplay-1", "letsplay", "idle", None, done=1)

    # ---------- запуск ----------

    def execute(self):
        self.log("scheduler", f"Run #{self.id} начат", stage="select")
        client = MetacriticClient()
        try:
            with pool.connection() as conn, conn.transaction():
                batch, source = crawler.select_batch(conn, client, self.day, settings.games_per_run)
                conn.execute("UPDATE runs SET source = %s, games_total = %s WHERE id = %s", (source, len(batch), self.id))
        finally:
            client.close()
        self.log("scheduler", f"Источник: {source}, игр: {len(batch)}", stage="select",
                 data={"slugs": [s for s, _ in batch]})
        threads = [threading.Thread(target=self.collector, args=(batch,), daemon=True)]
        threads += [threading.Thread(target=self.summarizer, args=(n,), daemon=True) for n in range(1, settings.llm_workers + 1)]
        threads += [threading.Thread(target=self.matcher, daemon=True), threading.Thread(target=self.letsplayer, daemon=True)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with pool.connection() as conn:
            n = similar.refresh(conn)  # новые игры могли стать соседями старых
            run = conn.execute("UPDATE runs SET status = 'done', finished_at = now() WHERE id = %s RETURNING *", (self.id,)).fetchone()
        self.log("scheduler", f"Run #{self.id} завершён: новых {run['games_new']}, обновлено {run['games_updated']}, "
                              f"ошибок {run['errors']}, ${float(run['cost_usd']):.4f}; похожие пересчитаны для {n} игр",
                 stage="done", level="ok")


def execute_run(run_id: int) -> None:
    try:
        Run(run_id).execute()
    except Exception as e:
        emit(run_id, "scheduler", f"Run #{run_id} упал: {e}", stage="done", level="error",
             data={"trace": traceback.format_exc(limit=5)})
        with pool.connection() as conn:
            conn.execute("UPDATE runs SET status = 'failed', finished_at = now(), error = %s WHERE id = %s", (str(e), run_id))
