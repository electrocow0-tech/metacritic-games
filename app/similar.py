"""Похожие игры из базы: косинус по признакам игры (как в прототипе), без векторной БД.

Блоки признаков (каждый нормализуется и входит со своим весом):
- жанры Metacritic, разработчик и издатель, платформы;
- TF-IDF слов описания (hashing trick, 2048 измерений);
- семантический эмбеддинг, если задан EMBEDDING_MODEL.
Для сотен и тысяч игр полный пересчёт занимает доли секунды.
"""
import hashlib
import json
import re

import numpy as np
from openai import OpenAI

from app.config import settings

TOP_K = 5
HASH_DIM = 2048
STOP = set("""a an the and or of to in on for with by from at as is are was were be been it its this that these those
you your yours his her their our we they he she them us i me my into over under than then up down out about after
before between through during while all any each every more most other some such no not only own same so too very can
will just also new game games play player players world experience one two way set""".split())
W_FEATURES = {"genre": 0.35, "text": 0.30, "company": 0.15, "platform": 0.20}
W_EMBED = 0.40  # доля семантического эмбеддинга, когда он есть

_embed_client = None


def tokens(text: str | None) -> list[str]:
    return [t for t in re.findall(r"[a-z][a-z0-9'-]{2,}", (text or "").lower()) if t not in STOP]


def _bucket(tok: str) -> int:
    return int(hashlib.md5(tok.encode()).hexdigest()[:8], 16) % HASH_DIM


def _norm_rows(m: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(m, axis=1, keepdims=True)
    return np.divide(m, n, out=np.zeros_like(m), where=n > 0)


def _onehot(rows: list[list[str]]) -> np.ndarray:
    vocab = {v: i for i, v in enumerate(sorted({x for r in rows for x in r}))}
    m = np.zeros((len(rows), max(1, len(vocab))), dtype=np.float32)
    for i, r in enumerate(rows):
        for x in r:
            m[i, vocab[x]] = 1
    return m


def build_matrix(games: list[dict]) -> tuple[np.ndarray, list[set[str]]]:
    """Матрица признаков (строки L2-нормированы) и множества токенов для объяснений."""
    toks = [set(tokens(g["description"])) for g in games]
    df = np.zeros(HASH_DIM, dtype=np.float32)
    for ts in toks:
        for b in {_bucket(t) for t in ts}:
            df[b] += 1
    idf = np.log((1 + len(games)) / (1 + df)) + 1
    text = np.zeros((len(games), HASH_DIM), dtype=np.float32)
    for i, ts in enumerate(toks):
        for t in ts:
            text[i, _bucket(t)] = idf[_bucket(t)]
    blocks = {
        "genre": _onehot([g["genres"] for g in games]),
        "text": text,
        "company": _onehot([[f"d:{x}" for x in g["developer"]] + [f"p:{x}" for x in g["publisher"]] for g in games]),
        "platform": _onehot([g["platforms"] for g in games]),
    }
    has_emb = [g.get("embedding") for g in games]
    use_emb = settings.embeddings_enabled and sum(e is not None for e in has_emb) >= 2
    scale = (1 - W_EMBED) if use_emb else 1.0
    parts = [_norm_rows(blocks[k]) * np.sqrt(w * scale) for k, w in W_FEATURES.items()]
    if use_emb:
        dim = len(next(e for e in has_emb if e is not None))
        emb = np.array([e if e is not None and len(e) == dim else np.zeros(dim) for e in has_emb], dtype=np.float32)
        parts.append(_norm_rows(emb) * np.sqrt(W_EMBED))
    return np.hstack(parts), toks


def reasons(a: dict, b: dict, ta: set[str], tb: set[str]) -> list[str]:
    out = []
    shared_genres = [x for x in a["genres"] if x in b["genres"]]
    if shared_genres:
        out.append("Тот же жанр: " + ", ".join(shared_genres))
    dev = [x for x in a["developer"] if x in b["developer"]]
    if dev:
        out.append("Тот же разработчик: " + dev[0])
    elif set(a["publisher"]) & set(b["publisher"]):
        out.append("Тот же издатель: " + next(x for x in a["publisher"] if x in b["publisher"]))
    words = sorted(ta & tb, key=len, reverse=True)[:3]
    if words:
        out.append("Общие мотивы в описании: " + ", ".join(words))
    plats = [p for p in a["platforms"] if p in b["platforms"]]
    if plats:
        out.append("Есть на " + ", ".join(plats[:3]))
    return out[:4]


def load_games(conn) -> list[dict]:
    return list(conn.execute(
        """SELECT g.id, g.slug, g.title, g.genres, g.developer, g.publisher, g.description, g.embedding,
                  coalesce(array_agg(p.platform ORDER BY p.platform) FILTER (WHERE p.platform IS NOT NULL), '{}') AS platforms
           FROM games g LEFT JOIN game_platforms p ON p.game_id = g.id GROUP BY g.id ORDER BY g.id"""
    ))


def refresh(conn, only_ids: list[int] | None = None) -> int:
    """Пересчитывает top-K похожих для указанных игр (или для всех). Возвращает число обновлённых игр."""
    games = load_games(conn)
    if len(games) < 2:
        return 0
    F, toks = build_matrix(games)
    idx = [i for i, g in enumerate(games) if only_ids is None or g["id"] in set(only_ids)]
    S = F[idx] @ F.T
    with conn.transaction():
        for row, i in enumerate(idx):
            s = S[row].copy()
            s[i] = -1
            top = np.argsort(-s)[:TOP_K]
            conn.execute("DELETE FROM similar_games WHERE game_id = %s", (games[i]["id"],))
            for rank, j in enumerate(top, 1):
                if s[j] <= 0:
                    continue
                conn.execute(
                    "INSERT INTO similar_games (game_id, similar_id, rank, score, reasons) VALUES (%s, %s, %s, %s, %s::jsonb)",
                    (games[i]["id"], games[j]["id"], rank, float(s[j]),
                     json.dumps(reasons(games[i], games[j], toks[i], toks[j]), ensure_ascii=False)),
                )
    return len(idx)


def embed_text(g: dict, critic_verdict: str | None) -> str:
    return "\n".join(filter(None, [g["title"], ", ".join(g["genres"]), g["description"], critic_verdict]))


def embed(text: str) -> tuple[list[float], int]:
    """Семантический эмбеддинг через OpenAI-совместимый API. Возвращает (вектор, токены)."""
    global _embed_client
    if _embed_client is None:
        _embed_client = OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key, timeout=60, max_retries=2)
    r = _embed_client.embeddings.create(model=settings.embedding_model, input=text[:8000])
    return list(r.data[0].embedding), (r.usage.total_tokens if r.usage else 0)
