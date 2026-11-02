"""Клиент внутреннего JSON API Metacritic (тот же, которым пользуется сам сайт).

Вежливый режим: пауза между запросами, повторы с экспоненциальной паузой на 429/5xx.
"""
import hashlib
import re
import time
from dataclasses import dataclass, field

import httpx

from app.config import settings

BACKEND = "https://backend.metacritic.com"
SITE = "https://www.metacritic.com"
IMG = "https://www.metacritic.com/a/img/catalog"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


class MetacriticError(Exception):
    pass


@dataclass
class GameData:
    slug: str
    title: str
    release_date: str | None
    description: str | None
    genres: list[str]
    developer: list[str]
    publisher: list[str]
    rating: str | None
    cover_url: str | None
    video: dict | None
    critic: dict
    user: dict
    platforms: list[dict]
    critic_reviews: list[dict] = field(default_factory=list)
    user_reviews: list[dict] = field(default_factory=list)


def score_or_none(v):
    """Metacritic отдаёт 0 или null, когда оценки ещё нет (tbd)."""
    return None if v in (None, 0) else v


def review_key(source: str | None, date: str | None, quote: str) -> str:
    return hashlib.sha1(f"{source}|{date}|{quote}".encode()).hexdigest()[:20]


class MetacriticClient:
    def __init__(self, delay: float | None = None, on_retry=None):
        self.delay = settings.request_delay_sec if delay is None else delay
        self.on_retry = on_retry  # callback(url, status, wait) для лога мониторинга
        self._last = 0.0
        self.http = httpx.Client(headers={"User-Agent": UA, "Accept": "application/json, text/html"},
                                 timeout=30, follow_redirects=True)

    def close(self):
        self.http.close()

    def _get(self, url: str, params: dict | None = None, as_json: bool = True):
        for attempt in range(5):
            wait = self.delay - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.http.get(url, params=params)
            except httpx.HTTPError as e:
                status, err = None, e
            else:
                if r.status_code == 200:
                    return r.json() if as_json else r.text
                if r.status_code == 404:
                    raise MetacriticError(f"404 {url}")
                status, err = r.status_code, None
            backoff = 2 ** attempt
            if self.on_retry:
                self.on_retry(url, status or str(err), backoff)
            time.sleep(backoff)
        raise MetacriticError(f"failed after retries: {url}")

    # ---------- выборка игр ----------

    def new_releases(self) -> list[str]:
        """Slug'и раздела New Releases с главной страницы Games (в API аналога нет, берём из HTML)."""
        html = self._get(f"{SITE}/game/", as_json=False)
        i = html.find("New Releases")
        if i < 0:
            return []
        slugs = re.findall(r'href="/game/([a-z0-9-]+)/"', html[i:i + 80000])
        return list(dict.fromkeys(slugs))

    def see_all_page(self, page: int, per_page: int = 24) -> list[dict]:
        """Страница SEE ALL с сортировкой «новые» (browse/game/all/all/all-time/new)."""
        data = self._get(f"{BACKEND}/finder/metacritic/web", {
            "sortBy": "-releaseDate", "productType": "games", "page": page,
            "releaseYearMin": 1958, "releaseYearMax": 2100,
            "offset": (page - 1) * per_page, "limit": per_page,
        })
        return [{"slug": it["slug"], "title": it["title"], "releaseDate": it.get("releaseDate")}
                for it in data.get("data", {}).get("items", [])]

    # ---------- карточка игры ----------

    def game(self, slug: str) -> GameData:
        page = self._get(f"{BACKEND}/composer/metacritic/pages/games/{slug}/web")
        comp = {c["meta"]["componentName"]: (c.get("data") or {}) for c in page.get("components", [])}
        it = (comp.get("product") or {}).get("item")
        if not it:
            raise MetacriticError(f"no product for {slug}")
        companies = (it.get("production") or {}).get("companies") or []
        card = next((im for im in it.get("images") or [] if im.get("typeName") == "cardImage"), None)
        video = it.get("video") or {}
        platforms = []
        for p in it.get("platforms") or []:
            stats = self._get(f"{BACKEND}/reviews/metacritic/user/games/{slug}/platform/{p['slug']}/stats/web")
            us = (stats.get("data") or {}).get("item") or {}
            cs = p.get("criticScoreSummary") or {}
            platforms.append({
                "platform": p["name"], "platform_slug": p["slug"],
                "metascore": score_or_none(cs.get("score")), "critic_reviews": cs.get("reviewCount") or 0,
                "userscore": score_or_none(us.get("score")), "user_reviews": us.get("reviewCount") or 0,
            })
        critic = (comp.get("critic-score-summary") or {}).get("item") or {}
        user = (comp.get("user-score-summary") or {}).get("item") or {}
        return GameData(
            slug=slug, title=it["title"], release_date=it.get("releaseDate"), description=it.get("description"),
            genres=[g["name"] for g in it.get("genres") or []],
            developer=[c["name"] for c in companies if c.get("typeName") == "Developer"],
            publisher=[c["name"] for c in companies if c.get("typeName") == "Publisher"],
            rating=it.get("rating"),
            cover_url=f"{IMG}{card['bucketPath']}" if card and card.get("bucketPath") else None,
            video={"title": video.get("title"), "url": video.get("embedUrl"), "duration": video.get("duration")}
            if video.get("embedUrl") else None,
            critic={"score": score_or_none(critic.get("score")), "reviews": critic.get("reviewCount") or 0,
                    "sentiment": critic.get("sentiment")},
            user={"score": score_or_none(user.get("score")), "reviews": user.get("reviewCount") or 0,
                  "sentiment": user.get("sentiment")},
            platforms=platforms,
            critic_reviews=self.reviews(slug, "critic", settings.critic_reviews_max, step=10),
            user_reviews=self.reviews(slug, "user", settings.user_reviews_max, step=25),
        )

    def reviews(self, slug: str, kind: str, limit: int, step: int) -> list[dict]:
        out: list[dict] = []
        for offset in range(0, limit, step):
            data = self._get(f"{BACKEND}/reviews/metacritic/{kind}/games/{slug}/web",
                             {"offset": offset, "limit": step, "filterBySentiment": "all", "sort": "date"})
            items = (data.get("data") or {}).get("items") or []
            for r in items:
                quote = (r.get("quote") or "").strip()
                if not quote or r.get("spoiler"):
                    continue
                source = r.get("publicationName") or r.get("author")
                out.append({
                    "ext_key": review_key(source, r.get("date"), quote),
                    "source": source, "score": r.get("score"), "quote": quote[:2000],
                    "platform": (((r.get("reviewedProduct") or {}).get("platform")) or {}).get("name"),
                    "review_date": r.get("date"), "url": r.get("url"),
                })
            if len(items) < step:
                break
        return out[:limit]
