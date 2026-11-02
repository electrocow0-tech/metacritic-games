"""Резюме отзывов через OpenAI-совместимый API, отдельно для критиков и игроков.

Каждый тезис обязан опираться на дословную цитату из конкретного отзыва. Цитаты проверяются
программно: тезис с цитатой, которой нет в отзывах, отбрасывается (защита от галлюцинаций).
"""
import hashlib
import json
import re
from dataclasses import dataclass

from openai import OpenAI

from app.config import settings

MIN_REVIEWS = {"critic": 1, "user": 2}
MAX_REVIEWS_IN_PROMPT = 40

SYSTEM = """Ты аналитик игровых отзывов. По отзывам составь краткое резюме на русском языке.
Отвечай строго JSON-объектом:
{
  "verdict": "1-2 предложения: общий вывод",
  "tagline": "короткая фраза об игре до 60 символов (только для критиков, для игроков пустая строка)",
  "likes": [{"text": "тезис до 90 символов", "review": <номер отзыва>, "quote": "дословная цитата"}],
  "dislikes": [{"text": "...", "review": <номер>, "quote": "..."}]
}
Правила:
- likes: 2-4 пункта, dislikes: 0-3 пункта, только то, о чём действительно пишут.
- quote: точный непрерывный фрагмент текста указанного отзыва, 5-30 слов, без перевода и правок.
- Не придумывай факты, которых нет в отзывах. Без длинного тире, используй дефис или запятую."""


@dataclass
class Summary:
    verdict: str
    tagline: str
    likes: list[dict]
    dislikes: list[dict]
    reviews_used: int
    tokens_in: int
    tokens_out: int
    cost_usd: float
    dropped: int  # сколько тезисов отброшено проверкой цитат


def reviews_hash(reviews: list[dict]) -> str:
    keys = sorted(r["ext_key"] for r in reviews)
    return hashlib.sha1("|".join(keys).encode()).hexdigest()


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().strip("\"'«»….").lower()


def verify_items(items: list, reviews: list[dict]) -> tuple[list[dict], int]:
    """Оставляет только тезисы, чья цитата дословно найдена в отзывах; src берём из найденного отзыва."""
    out, dropped = [], 0
    texts = [_norm(r["quote"]) for r in reviews]
    for it in items or []:
        if not isinstance(it, dict) or not it.get("text") or not it.get("quote"):
            dropped += 1
            continue
        q = _norm(str(it["quote"]))
        idx = it.get("review")
        hit = None
        if isinstance(idx, int) and 1 <= idx <= len(reviews) and q and q in texts[idx - 1]:
            hit = idx - 1
        elif q:
            hit = next((i for i, t in enumerate(texts) if q in t), None)
        if hit is None or len(q) < 12:
            dropped += 1
            continue
        out.append({"text": str(it["text"])[:120], "quote": str(it["quote"]).strip(), "src": reviews[hit]["source"]})
    return out, dropped


class Summarizer:
    def __init__(self):
        self.client = OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key, timeout=90, max_retries=2)

    def summarize(self, title: str, kind: str, reviews: list[dict]) -> Summary | None:
        if len(reviews) < MIN_REVIEWS[kind]:
            return None
        used = reviews[:MAX_REVIEWS_IN_PROMPT]
        who = "критиков (рецензии изданий)" if kind == "critic" else "игроков (пользовательские отзывы)"
        lines = [f"[{i}] ({r.get('score')}) {r['quote']}" for i, r in enumerate(used, 1)]
        user_msg = f"Игра: {title}\nОтзывы {who}:\n" + "\n".join(lines)
        resp = self.client.chat.completions.create(
            model=settings.llm_model,
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user_msg}],
            response_format={"type": "json_object"},
            temperature=0.2,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        likes, d1 = verify_items(data.get("likes"), used)
        dislikes, d2 = verify_items(data.get("dislikes"), used)
        usage = resp.usage
        tin, tout = (usage.prompt_tokens, usage.completion_tokens) if usage else (0, 0)
        cost = (tin * settings.llm_price_in + tout * settings.llm_price_out) / 1e6
        return Summary(
            verdict=str(data.get("verdict") or "").strip(), tagline=str(data.get("tagline") or "").strip()[:80],
            likes=likes, dislikes=dislikes, reviews_used=len(used),
            tokens_in=tin, tokens_out=tout, cost_usd=cost, dropped=d1 + d2,
        )
