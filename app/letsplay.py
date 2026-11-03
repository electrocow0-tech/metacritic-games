"""Летсплей (доп. часть 1): самый популярный ролик на YouTube, текст из автосубтитров, заключение с таймкодами.

- Поиск ролика и метаданные: yt-dlp (ytsearch, без скачивания видео).
- Автосубтитры: youtube-transcript-api. YouTube отдаёт их только на residential IP, поэтому на сервере
  запросы идут через прокси из YOUTUBE_PROXY. Без прокси или без субтитров сохраняется ссылка на ролик.
- Заключение и ключевые моменты: тот же LLM, что и для резюме.
Темп обращений к YouTube: не чаще 1 запроса в секунду.
"""
import json
import re
import threading
import time
import unicodedata
from datetime import datetime, timedelta

import yt_dlp
from openai import OpenAI
from youtube_transcript_api import (IpBlocked, NoTranscriptFound, RequestBlocked, TranscriptsDisabled,
                                    YouTubeTranscriptApi)
from youtube_transcript_api.proxies import GenericProxyConfig

from app.config import settings
from app.db import pool
from app.runs import bump
from app.summarizer import tidy

SEARCH_N = 20
MIN_DURATION = 300          # отсекаем шортсы и короткие трейлеры
REFRESH_AFTER = timedelta(days=7)
MAX_TRANSCRIPT_CHARS = 14000
YT_INTERVAL = 1.0           # секунд между запросами к YouTube
BLOCK_PAUSE = timedelta(minutes=30)
YDL_OPTS = {"quiet": True, "no_warnings": True, "skip_download": True,
            **({"proxy": settings.youtube_proxy} if settings.youtube_proxy else {})}
TITLE_STOP = {"the", "of", "and", "a", "an", "to", "in", "on", "for", "edition", "remastered", "remaster", "deluxe",
              "collection", "definitive", "complete", "game", "goty"}

SYSTEM = """Ты пересказываешь игровой летсплей по расшифровке речи блогера (автосубтитры, с таймкодами [мм:сс]).
Ответь строго JSON-объектом на русском языке:
{"summary": "2-3 предложения: к какому выводу приходит автор, что ему понравилось и что нет",
 "moments": [{"t": <секунда начала из таймкода>, "text": "что происходит, до 70 символов"}]}
moments: 5-6 ключевых моментов по возрастанию времени, t только из таймкодов расшифровки.
Не выдумывай того, чего нет в расшифровке. Без длинного тире."""

_lock = threading.Lock()
_last_call = 0.0
_blocked_until: datetime | None = None   # YouTube ограничил запросы (429 или блок): короткая пауза


class NoSubtitles(Exception):
    """У ролика нет автосубтитров: это не блокировка, ссылку сохраняем без заключения."""


class SubtitlesBlocked(Exception):
    """YouTube не отдал субтитры этому IP (429, блокировка)."""


def _pace() -> None:
    global _last_call
    with _lock:
        wait = YT_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def subtitles_paused() -> bool:
    return _blocked_until is not None and datetime.now() < _blocked_until


# ---------- поиск ----------

def _plain(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def key_words(title: str) -> list[str]:
    """Значимые слова основной части названия (до двоеточия или « - »)."""
    main = re.split(r":| - | – ", title)[0]
    words = [w for w in _plain(main).split() if w not in TITLE_STOP and (len(w) >= 3 or w.isdigit())]
    return words or _plain(main).split()


def is_relevant(game_title: str, video_title: str | None) -> bool:
    """Ролик про эту игру: в его названии есть все значимые слова названия игры."""
    vt = f" {_plain(video_title or '')} "
    return all(f" {w} " in vt for w in key_words(game_title))


def find_video(title: str) -> dict | None:
    """Самый просматриваемый ролик длиннее 5 минут среди тех, что действительно про эту игру."""
    _pace()
    with yt_dlp.YoutubeDL({**YDL_OPTS, "extract_flat": True}) as ydl:
        res = ydl.extract_info(f'ytsearch{SEARCH_N}:"{title}" gameplay', download=False)
    entries = [e for e in res.get("entries") or [] if e and e.get("id") and is_relevant(title, e.get("title"))]
    long = [e for e in entries if (e.get("duration") or 0) >= MIN_DURATION] or entries
    if not long:
        return None
    e = max(long, key=lambda e: e.get("view_count") or 0)
    return {"id": e["id"], "title": e.get("title"), "channel": e.get("channel") or e.get("uploader"),
            "views": e.get("view_count"), "duration": int(e["duration"]) if e.get("duration") else None}


# ---------- субтитры ----------

def transcript(video_id: str) -> list[tuple[int, str]]:
    """Автосубтитры ролика: английские, иначе любая автоматическая дорожка. [(секунда, текст)]."""
    global _blocked_until
    proxy = settings.youtube_proxy
    api = YouTubeTranscriptApi(proxy_config=GenericProxyConfig(http_url=proxy, https_url=proxy) if proxy else None)
    try:
        _pace()
        tracks = api.list(video_id)
        try:
            tr = tracks.find_generated_transcript(["en"])
        except NoTranscriptFound:
            tr = next((t for t in tracks if t.is_generated), None)
            if tr is None:
                raise
        _pace()
        fetched = tr.fetch()
    except (NoTranscriptFound, TranscriptsDisabled) as e:
        raise NoSubtitles(type(e).__name__) from e
    except (RequestBlocked, IpBlocked) as e:
        _blocked_until = datetime.now() + BLOCK_PAUSE
        raise SubtitlesBlocked(type(e).__name__) from e
    except Exception as e:
        if "429" in str(e):
            _blocked_until = datetime.now() + BLOCK_PAUSE
            raise SubtitlesBlocked("HTTP 429") from e
        raise
    return [(int(s.start), s.text.replace("\n", " ").strip()) for s in fetched if s.text.strip()]


def condense(segs: list[tuple[int, str]], window: int = 30) -> str:
    """Склеивает субтитры в строки по 30 секунд с таймкодом; длинные расшифровки равномерно прореживаются."""
    lines, cur_t, buf = [], None, []
    for t, text in segs:
        if cur_t is None or t - cur_t >= window:
            if buf:
                lines.append(f"[{cur_t // 60:02d}:{cur_t % 60:02d}] {' '.join(buf)}")
            cur_t, buf = t, []
        buf.append(text)
    if buf:
        lines.append(f"[{cur_t // 60:02d}:{cur_t % 60:02d}] {' '.join(buf)}")
    total = sum(len(x) for x in lines)
    if total > MAX_TRANSCRIPT_CHARS:
        step = total / MAX_TRANSCRIPT_CHARS
        lines = [x for i, x in enumerate(lines) if int(i % step) == 0 or i == 0]
    return "\n".join(lines)


# ---------- заключение ----------

def summarize(title: str, text: str, duration: int | None) -> tuple[dict, int, int]:
    client = OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key, timeout=120, max_retries=2)
    resp = client.chat.completions.create(
        model=settings.llm_model, temperature=0.2, response_format={"type": "json_object"},
        messages=[{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": f"Игра: {title}\nРасшифровка:\n{text}"}],
    )
    data = json.loads(resp.choices[0].message.content or "{}")
    moments = []
    for m in data.get("moments") or []:
        try:
            t = int(m["t"])
        except (KeyError, TypeError, ValueError):
            continue
        if t >= 0 and (not duration or t <= duration) and m.get("text"):
            moments.append({"t": t, "text": tidy(m["text"])[:90]})
    u = resp.usage
    return ({"summary": tidy(data.get("summary")), "moments": sorted(moments, key=lambda m: m["t"])[:6]},
            u.prompt_tokens if u else 0, u.completion_tokens if u else 0)


def process(run, item: dict, force: bool = False, reuse_video: bool = False) -> None:
    """force: не смотреть на свежесть; reuse_video: если сохранённый ролик релевантен, не искать заново."""
    name = "letsplay-1"
    with pool.connection() as conn:
        old = conn.execute("SELECT * FROM letsplays WHERE game_id = %s", (item["id"],)).fetchone()
    if not force and old and old["updated_at"] > datetime.now(old["updated_at"].tzinfo) - REFRESH_AFTER:
        run.log(name, f"«{item['title']}»: летсплей свежий, пропуск", stage="letsplay", slug=item["slug"])
        return
    if reuse_video and old and is_relevant(item["title"], old["title"]):
        video = {"id": old["video_id"], "title": old["title"], "channel": old["channel"], "views": old["views"],
                 "duration": old["duration_sec"]}
    else:
        video = find_video(item["title"])
    if not video:
        with pool.connection() as conn:  # старая нерелевантная ссылка не должна остаться
            conn.execute("DELETE FROM letsplays WHERE game_id = %s", (item["id"],))
        run.log(name, f"«{item['title']}»: ролика именно про эту игру не нашлось", stage="letsplay", slug=item["slug"])
        return

    segs, problem, level = [], None, "info"
    if subtitles_paused():
        problem, level = "YouTube ограничил запросы, субтитры на паузе 30 минут", "warn"
    else:
        try:
            segs = transcript(video["id"])
        except NoSubtitles:
            problem = "у ролика нет автосубтитров"
        except SubtitlesBlocked as e:
            problem, level = f"YouTube не отдал субтитры ({e}), пауза 30 минут", "warn"
        except Exception as e:  # ссылку на найденный ролик сохраняем при любой ошибке субтитров
            problem, level = f"субтитры не получены: {str(e)[:100]}", "warn"
    text = condense(segs)

    result, tin, tout = {"summary": None, "moments": []}, 0, 0
    if text and not settings.llm_enabled:
        problem = "LLM не настроен"
    elif text:
        try:
            result, tin, tout = summarize(item["title"], text, video.get("duration"))
        except Exception as e:
            problem, level = f"заключение не получено: {str(e)[:100]}", "warn"
    cost = (tin * settings.llm_price_in + tout * settings.llm_price_out) / 1e6
    with pool.connection() as conn:
        conn.execute(
            """INSERT INTO letsplays (game_id, video_id, title, channel, views, duration_sec, summary, moments, transcript_chars, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
               ON CONFLICT (game_id) DO UPDATE SET video_id = EXCLUDED.video_id, title = EXCLUDED.title, channel = EXCLUDED.channel,
                 views = EXCLUDED.views, duration_sec = EXCLUDED.duration_sec, summary = EXCLUDED.summary,
                 moments = EXCLUDED.moments, transcript_chars = EXCLUDED.transcript_chars, updated_at = now()""",
            (item["id"], video["id"], video["title"], video["channel"], video["views"], video["duration"],
             result["summary"], json.dumps(result["moments"], ensure_ascii=False), len(text)),
        )
    if tin:
        bump(run.id, tokens_in=tin, tokens_out=tout, cost_usd=cost)
    views = f"{video['views'] or 0:,}".replace(",", " ")
    note = (f"заключение и {len(result['moments'])} моментов, {tin} → {tout} токенов, ${cost:.4f}"
            if result["summary"] else f"только ссылка: {problem or 'пустая расшифровка'}")
    run.log(name, f"«{item['title']}»: {views} просмотров, {note}", stage="letsplay", slug=item["slug"], level=level,
            data={"tokens_in": tin, "tokens_out": tout, "cost": cost, "title": item["title"]} if tin else None)
