"""Собирает данные прототипа и вшивает их в docs/index.html.

Вход (tools/prototype/.cache):
  games20.json   - игры из внутреннего API Metacritic (collect.py + reviews.py)
  ai_in_*.json   - отзывы, переданные в LLM-разметку
  ai_out_*.json  - LLM-слой: резюме, аспекты с цитатами, теги, демо-летсплей
  finder.json    - первая страница SEE ALL (для симуляции мониторинга)
Выход: блок <!--DATA-->...<!--/DATA--> в docs/index.html.
"""
import datetime as dt
import json
import re
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
CACHE = Path(__file__).resolve().parent / ".cache"
HTML = ROOT / "docs" / "index.html"
MSK = dt.timezone(dt.timedelta(hours=3))
CLUSTER_COLORS = ["#c8ff3d", "#ffb547", "#a78bfa", "#34d399", "#60a5fa", "#f472b6"]
N_CLUSTERS = 6
ASPECTS = ["gameplay", "graphics", "story", "performance", "price", "multiplayer"]
GENERIC_TAGS = {"single-player", "indie", "action", "adventure"}
TAG_RU = {
    "action": "экшен", "adventure": "приключения", "rpg": "RPG", "jrpg": "JRPG", "tactics": "тактика",
    "turn-based": "пошаговые", "shooter": "шутеры", "racing": "гонки", "flight": "авиа", "platformer": "платформеры",
    "metroidvania": "метроидвании", "survival": "выживание", "horror": "хоррор", "party": "вечеринки",
    "puzzle": "головоломки", "point-and-click": "квесты", "narrative": "нарратив", "open-world": "открытый мир",
    "crafting": "крафт", "co-op": "кооператив", "multiplayer": "мультиплеер", "story-rich": "сюжет",
    "souls-like": "souls-like", "melee-combat": "ближний бой", "stealth": "стелс", "exploration": "исследование",
    "retro": "ретро", "remaster": "ремастеры", "compilation": "сборники", "pixel-art": "пиксель-арт",
    "family": "семейные", "sci-fi": "sci-fi", "fantasy": "фэнтези", "historical": "история", "dinosaurs": "динозавры",
    "cozy": "уютные", "mystery": "детективы", "dark": "мрачные", "arcade": "аркады", "anime": "аниме",
    "licensed": "франшизы", "base-building": "базы", "dlc": "дополнения",
}


def load(name):
    return json.loads((CACHE / name).read_text(encoding="utf-8"))


def score_or_none(v):
    # Metacritic отдаёт 0, когда оценки ещё нет (tbd): храним как null, а не как ноль
    return v if v not in (0, None) else None


def jaccard(a, b):
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if a | b else 0.0


def aspect_sim(x, y):
    diffs = [abs(x[k]["critic"] - y[k]["critic"]) for k in ASPECTS
             if x[k]["critic"] is not None and y[k]["critic"] is not None]
    if not diffs:
        return 0.0
    return max(0.0, 1 - (sum(diffs) / len(diffs)) / 3)


def kmeans(X, k, seed=7, iters=60, restarts=20):
    rng = np.random.default_rng(seed)
    best = None
    for _ in range(restarts):
        C = X[rng.choice(len(X), k, replace=False)]
        for _ in range(iters):
            lab = np.argmin(((X[:, None] - C[None]) ** 2).sum(-1), 1)
            C2 = np.array([X[lab == j].mean(0) if (lab == j).any() else C[j] for j in range(k)])
            if np.allclose(C, C2):
                break
            C = C2
        inertia = ((X - C[lab]) ** 2).sum()
        if best is None or inertia < best[0]:
            best = (inertia, lab)
    return best[1]


def mds(D, dims):
    """Классический MDS: координаты, сохраняющие попарные расстояния."""
    m = len(D)
    if m == 1:
        return np.zeros((1, dims))
    J = np.eye(m) - np.ones((m, m)) / m
    with np.errstate(all="ignore"):  # ложные предупреждения Accelerate BLAS на macOS
        B = -0.5 * J @ (D ** 2) @ J
    assert np.isfinite(B).all()
    w, v = np.linalg.eigh(B)
    order = np.argsort(w)[::-1][:dims]
    out = v[:, order] * np.sqrt(np.maximum(w[order], 1e-9))
    if out.shape[1] < dims:
        out = np.hstack([out, np.zeros((m, dims - out.shape[1]))])
    return out


def cluster_layout(D, lab, W, H, lw, lh, transpose=False):
    """Раскладка карты в пикселях холста W x H.

    Центры кластеров берутся из MDS межкластерных расстояний, точки внутри кластера из локального MDS.
    Затем круги кластеров раздвигаются без перекрытий (с местом под заголовок), а точки внутри
    кластера расталкиваются с учётом ширины подписи lw и высоты строки lh.
    """
    ks = sorted(set(lab))
    idx = {c: [i for i in range(len(lab)) if lab[i] == c] for c in ks}
    CD = np.array([[D[np.ix_(idx[a], idx[b])].mean() if a != b else 0 for b in ks] for a in ks])
    C = mds(CD, 2)
    if transpose:
        C = C[:, ::-1]
    C = (C - C.min(0)) / (C.max(0) - C.min(0) + 1e-9)
    pad, title = 24, 34
    rad = np.array([lw * 0.55 + lh * 0.9 * len(idx[c]) ** 0.85 for c in ks])
    C = np.column_stack([pad + rad.max() + C[:, 0] * (W - 2 * pad - 2 * rad.max()),
                         pad + title + rad.max() + C[:, 1] * (H - 2 * pad - title - 2 * rad.max())])
    home = C.copy()
    for it in range(800):
        for a in range(len(ks)):
            for b in range(a + 1, len(ks)):
                d = C[a] - C[b]
                dist = np.hypot(*d) + 1e-9
                need = rad[a] + rad[b] + title
                if dist < need:
                    shift = (need - dist) / 2 * d / dist
                    C[a] += shift
                    C[b] -= shift
        if it < 600:  # притяжение к MDS-позициям только в начале, в конце остаётся чистое расталкивание
            C += (home - C) * 0.01
    X = np.zeros((len(lab), 2))
    for ci, c in enumerate(ks):
        mem = idx[c]
        L = mds(D[np.ix_(mem, mem)], 2) if len(mem) > 1 else np.zeros((1, 2))
        if transpose:
            L = L[:, ::-1]
        L = L / (np.abs(L).max() + 1e-9) * rad[ci] * 0.5
        lim = rad[ci] - lh * 0.8
        for _ in range(400):
            for a in range(len(mem)):
                for b in range(a + 1, len(mem)):
                    d = L[a] - L[b]
                    # эллиптическая метрика: подписи широкие, поэтому по горизонтали нужен больший зазор
                    dn = np.array([d[0] / (lw / lh), d[1]])
                    dist = np.hypot(*dn) + 1e-9
                    if dist < lh:
                        dirv = d / (np.hypot(*d) + 1e-9) if np.hypot(*d) > 1e-6 else np.array([1.0, 0.3])
                        step = (lh - dist) / 2 * np.array([lw / lh, 1.0]) * 0.5
                        L[a] += dirv * step
                        L[b] -= dirv * step
            for a in range(len(mem)):  # не выходим за круг кластера
                r = np.hypot(*L[a])
                if r > lim:
                    L[a] *= lim / r
        X[mem] = C[ci] + L
    # viewBox подгоняется под раскладку: круги никогда не обрезаются и не накладываются
    x0, y0 = (C - rad[:, None]).min(0) - [pad, pad + title]
    x1, y1 = (C + rad[:, None]).max(0) + pad
    C, X = C - [x0, y0], X - [x0, y0]
    clusters = {c: {"cx": round(float(C[i, 0]), 1), "cy": round(float(C[i, 1]), 1), "r": round(float(rad[i]), 1)}
                for i, c in enumerate(ks)}
    clusters["box"] = [round(float(x1 - x0)), round(float(y1 - y0))]
    return X, clusters


def main():
    raw = load("games20.json")
    ai = {g["slug"]: g for n in (1, 2, 3) for g in load(f"ai_out_{n}.json")}
    fed = {g["slug"]: g for n in (1, 2, 3) for g in load(f"ai_in_{n}.json")}
    collected_at = dt.datetime.fromtimestamp((CACHE / "home.html").stat().st_mtime, MSK)

    games = []
    for r in raw:
        a = ai[r["slug"]]
        crit, usr = r.get("critic") or {}, r.get("user") or {}
        games.append({
            "slug": r["slug"], "title": r["title"], "releaseDate": r["releaseDate"],
            "description": r["description"], "genres": r["genres"], "rating": r.get("rating"),
            "developer": r["developer"], "publisher": r["publisher"],
            "metascore": score_or_none(crit.get("score")), "userscore": score_or_none(usr.get("score")),
            "criticSentiment": crit.get("sentiment"), "userSentiment": usr.get("sentiment"),
            "criticTotal": crit.get("reviewCount"), "userTotal": usr.get("reviewCount"),
            "analyzed": {"critic": len(fed[r["slug"]]["criticReviews"]), "user": len(fed[r["slug"]]["userReviews"])},
            "platforms": [{"name": p["name"], "metascore": score_or_none(p["metascore"]),
                           "criticReviews": p["criticReviews"], "userscore": score_or_none(p["userscore"]),
                           "userReviews": p["userReviews"]} for p in r["platforms"]],
            "video": r["video"] if r.get("video") and r["video"].get("embedUrl") else None,
            "inNewReleases": r["inNewReleases"],
            **{k: a[k] for k in ("palette", "pattern", "tagline", "tags", "summary", "aspects", "letsplay")},
        })

    # похожие: теги + жанр + профиль аспектов + платформы
    n = len(games)
    S = np.zeros((n, n))
    sims = {g["slug"]: [] for g in games}
    for i, x in enumerate(games):
        for j, y in enumerate(games):
            if i == j:
                continue
            t = jaccard(x["tags"], y["tags"])
            asp = aspect_sim(x["aspects"], y["aspects"])
            px, py = {p["name"] for p in x["platforms"]}, {p["name"] for p in y["platforms"]}
            same_genre = bool(set(x["genres"]) & set(y["genres"]))
            same_dev = bool(set(x["developer"]) & set(y["developer"]))
            s = 0.55 * t + 0.15 * same_genre + 0.2 * asp + 0.05 * jaccard(px, py) + 0.05 * same_dev
            S[i, j] = s
            sims[x["slug"]].append({
                "slug": y["slug"], "score": round(min(0.99, 0.25 + s), 2),
                "sharedTags": [tg for tg in x["tags"] if tg in y["tags"] and tg not in ("single-player",)],
                "sameGenre": same_genre, "sameDev": same_dev, "aspectSim": round(asp, 2),
                "sharedPlatforms": sorted(px & py),
            })
    for g in games:
        g["similar"] = sorted(sims[g["slug"]], key=lambda s: -s["score"])[:5]

    # карта: классический MDS по расстоянию 1 - сходство
    D = 1 - (S + S.T) / 2
    np.fill_diagonal(D, 0)
    emb = mds(D, 6)
    # кластеры считаем в 6 измерениях, а раскладку строим с учётом кластеров
    lab = kmeans(emb, N_CLUSTERS)
    # две раскладки: горизонтальный холст для десктопа и вертикальный для телефона (крупнее подписи)
    Xd, Cd = cluster_layout(D, lab, 1000, 640, lw=118, lh=44)

    k = N_CLUSTERS
    overall = {}
    for g in games:
        for tg in g["tags"]:
            overall[tg] = overall.get(tg, 0) + 1
    clusters = []
    for c in range(k):
        members = [games[i] for i in range(n) if lab[i] == c]
        freq = {}
        for g in members:
            for tg in g["tags"]:
                if tg not in GENERIC_TAGS:
                    freq[tg] = freq.get(tg, 0) + 1
        # TF-IDF: частый внутри кластера и редкий в базе тег лучше описывает группу
        tfidf = {t: cnt * np.log(n / overall[t]) for t, cnt in freq.items() if cnt >= 2 or len(members) == 1}
        top = [t for t, _ in sorted(tfidf.items(), key=lambda kv: (-kv[1], kv[0]))[:2]]
        name = " и ".join(TAG_RU.get(t, t) for t in top) or "разное"
        clusters.append({"id": c, "name": name[0].upper() + name[1:], "color": CLUSTER_COLORS[c], "d": Cd[c]})
    for i, g in enumerate(games):
        g["map"] = {"cluster": int(lab[i]), "d": [round(float(Xd[i, 0]), 1), round(float(Xd[i, 1]), 1)]}

    see_all = [it["title"] for it in load("finder.json")["data"]["items"]]
    data = {"mapBox": Cd["box"], "collectedAt": collected_at.isoformat(timespec="minutes"), "source": "backend.metacritic.com",
            "games": games, "clusters": clusters, "seeAll": see_all}
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = HTML.read_text(encoding="utf-8")
    html = re.sub(r"<!--DATA-->.*?<!--/DATA-->",
                  lambda _: f'<!--DATA--><script id="db" type="application/json">{blob}</script><!--/DATA-->',
                  html, count=1, flags=re.S)
    HTML.write_text(html, encoding="utf-8")
    print(f"games={n} clusters={[c['name'] for c in clusters]} collectedAt={data['collectedAt']} size={len(html)//1024}KB")


if __name__ == "__main__":
    main()
