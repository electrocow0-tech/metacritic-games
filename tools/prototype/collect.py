"""Collect recent games from Metacritic internal JSON API for the prototype."""
import json, re, sys, time, urllib.request

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
B = "https://backend.metacritic.com"
out_dir = sys.argv[1]

def get(url):
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            time.sleep(2 * (attempt + 1))
    return None

home = open(f"{out_dir}/home.html", encoding="utf-8").read()
i = home.find("New Releases")
new_rel = list(dict.fromkeys(re.findall(r'href="/game/([a-z0-9-]+)/"', home[i:i + 60000])))
cands = list(new_rel)
# recent releases sorted by metascore
for page in range(1, 4):
    f = get(f"{B}/finder/metacritic/web?sortBy=-metaScore&productType=games&page={page}&releaseYearMin=2026&releaseYearMax=2026&offset={(page-1)*24}&limit=24")
    for it in (f or {}).get("data", {}).get("items", []):
        if it.get("releaseDate", "") >= "2026-09-15":
            cands.append(it["slug"])
cands = list(dict.fromkeys(cands))
print("candidates:", len(cands), file=sys.stderr)

games = []
for slug in cands:
    d = get(f"{B}/composer/metacritic/pages/games/{slug}/web")
    time.sleep(0.7)
    if not d:
        continue
    comp = {c["meta"]["componentName"]: c.get("data") or {} for c in d["components"]}
    it = comp["product"]["item"]
    if not it.get("releaseDate") or it["releaseDate"] > "2026-11-02" or not (it.get("criticScoreSummary") or {}).get("score"):
        print("skip", slug, it.get("releaseDate"), file=sys.stderr)
        continue
    plats = []
    for p in it.get("platforms") or []:
        us = get(f"{B}/reviews/metacritic/user/games/{slug}/platform/{p['slug']}/stats/web")
        time.sleep(0.4)
        usi = (us or {}).get("data", {}).get("item", {})
        cs = p.get("criticScoreSummary") or {}
        plats.append({"name": p["name"], "slug": p["slug"], "metascore": cs.get("score"), "criticReviews": cs.get("reviewCount"),
                      "userscore": usi.get("score"), "userReviews": usi.get("reviewCount"), "lead": p.get("isLeadPlatform")})
    prod = it.get("production") or {}
    comps = prod.get("companies") or []
    cr = comp.get("critic-reviews", {}).get("item", {}) or {}
    ur = comp.get("user-reviews", {}).get("item", {}) or {}
    def crit(lst):
        return [{"q": r["quote"], "score": r["score"], "pub": r.get("publicationName"), "platform": ((r.get("reviewedProduct") or {}).get("platform") or {}).get("name")} for r in (lst or [])[:12] if r.get("quote")]
    def usr(lst):
        return [{"q": r["quote"][:600], "score": r["score"], "author": r.get("author"), "platform": ((r.get("reviewedProduct") or {}).get("platform") or {}).get("name")} for r in (lst or [])[:12] if r.get("quote") and not r.get("spoiler")]
    v = it.get("video") or {}
    games.append({
        "slug": slug, "title": it["title"], "releaseDate": it["releaseDate"], "description": it.get("description"),
        "genres": [g["name"] for g in it.get("genres") or []], "rating": it.get("rating"),
        "developer": [c["name"] for c in comps if c.get("typeName") == "Developer"],
        "publisher": [c["name"] for c in comps if c.get("typeName") == "Publisher"],
        "critic": comp.get("critic-score-summary", {}).get("item"), "user": comp.get("user-score-summary", {}).get("item"),
        "platforms": plats,
        "video": {"title": v.get("title"), "duration": v.get("duration"), "embedUrl": v.get("embedUrl")} if v else None,
        "criticReviews": {"all": crit(cr.get("default")), "pos": crit(cr.get("positive")), "neg": crit(cr.get("negative")), "mixed": crit(cr.get("neutral"))},
        "userReviews": {"all": usr(ur.get("default")), "pos": usr(ur.get("positive")), "neg": usr(ur.get("negative")), "mixed": usr(ur.get("neutral"))},
        "inNewReleases": slug in new_rel,
    })
    print("ok", slug, it["releaseDate"], it["criticScoreSummary"]["score"], file=sys.stderr)
    if len(games) >= 22:
        break

json.dump(games, open(f"{out_dir}/games_raw.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(len(games))
