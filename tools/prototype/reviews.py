"""Fetch extra critic/user reviews for collected games (paginated)."""
import json, sys, time, urllib.request
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
B = "https://backend.metacritic.com"
d = sys.argv[1]
games = json.load(open(f"{d}/games_raw.json", encoding="utf-8"))

def get(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None

for g in games:
    out = {}
    for kind, step, maxn in (("critic", 10, 30), ("user", 25, 50)):
        items = []
        for off in range(0, maxn, step):
            r = get(f"{B}/reviews/metacritic/{kind}/games/{g['slug']}/web?offset={off}&limit={step}&filterBySentiment=all&sort=date")
            time.sleep(0.5)
            batch = (r or {}).get("data", {}).get("items", [])
            items += batch
            if len(batch) < step:
                break
        out[kind] = [{"q": x["quote"][:700], "score": x.get("score"),
                      "src": x.get("publicationName") or x.get("author"),
                      "platform": ((x.get("reviewedProduct") or {}).get("platform") or {}).get("name")}
                     for x in items if x.get("quote") and not x.get("spoiler")]
    g["reviewsFull"] = out
    print(g["slug"], len(out["critic"]), len(out["user"]), file=sys.stderr)
json.dump(games, open(f"{d}/games_raw.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
