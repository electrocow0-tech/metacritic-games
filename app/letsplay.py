"""Летсплей (доп. часть 1): самый популярный ролик на YouTube, текст из автосубтитров, заключение с таймкодами.

Поиск и субтитры через yt-dlp (без YouTube Data API и без скачивания видео), заключение через тот же LLM.
"""
import json
import re
import unicodedata
from datetime import datetime, timedelta

import yt_dlp
from openai import OpenAI

from app.config import settings
from app.db import pool
from app.runs import bump
from app.summarizer import tidy

SEARCH_N = 20
MIN_DURATION = 300          # отсекаем шортсы и короткие трейлеры
REFRESH_AFTER = timedelta(days=7)
MAX_TRANSCRIPT_CHARS = 14000
YDL_OPTS = {"quiet": True, "no_warnings": True, "skip_download": True,
            **({"proxy": settings.youtube_proxy} if settings.youtube_proxy else {}),
            # YouTube требует PO token для субтитров. С сервером bgutil-ytdlp-pot-provider токен попадает в URL
            # субтитров веб-клиента; видеоформаты при этом не нужны (без JS runtime их нет)
            **({"ignore_no_formats_error": True,
                "extractor_args": {"youtubepot-bgutilhttp": {"base_url": [settings.bgutil_url]},
                                   "youtube": {"fetch_pot": ["always"], "player_client": ["web"]}}}
               if settings.bgutil_url else {})}


class SubtitlesUnavailable(Exception):
    pass


BLOCK_PAUSE = timedelta(hours=6)
_blocked_until: datetime | None = None   # YouTube заблокировал IP (429 или проверка на бота): не дёргаем его зря


def subtitles_paused() -> bool:
    return _blocked_until is not None and datetime.now() < _blocked_until


def _remember_block(msg: str) -> None:
    global _blocked_until
    if "429" in msg or "not a bot" in msg or "Sign in" in msg:
        _blocked_until = datetime.now() + BLOCK_PAUSE

SYSTEM = """Ты пересказываешь игровой летсплей по расшифровке речи блогера (автосубтитры, с таймкодами [мм:сс]).
Ответь строго JSON-объектом на русском языке:
{"summary": "2-3 предложения: к какому выводу приходит автор, что ему понравилось и что нет",
 "moments": [{"t": <секунда начала из таймкода>, "text": "что происходит, до 70 символов"}]}
moments: 5-6 ключевых моментов по возрастанию времени, t только из таймкодов расшифровки.
Не выдумывай того, чего нет в расшифровке. Без длинного тире."""


TITLE_STOP = {"the", "of", "and", "a", "an", "to", "in", "on", "for", "edition", "remastered", "remaster", "deluxe",
              "collection", "definitive", "complete", "game", "goty"}


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
    with yt_dlp.YoutubeDL({**YDL_OPTS, "extract_flat": True}) as ydl:
        res = ydl.extract_info(f'ytsearch{SEARCH_N}:"{title}" gameplay', download=False)
    entries = [e for e in res.get("entries") or [] if e and e.get("id") and is_relevant(title, e.get("title"))]
    long = [e for e in entries if (e.get("duration") or 0) >= MIN_DURATION] or entries
    return max(long, key=lambda e: e.get("view_count") or 0) if long else None


def transcript(video_id: str) -> tuple[list[tuple[int, str]], dict]:
    """Автосубтитры на языке оригинала (дорожка *-orig), иначе английские. Возвращает [(секунда, текст)] и метаданные."""
    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        caps = info.get("automatic_captions") or {}
        subs = info.get("subtitles") or {}
        lang = next((k for k in caps if k.endswith("-orig")), None)
        tracks = caps.get(lang) if lang else (subs.get("en") or caps.get("en") or subs.get("ru") or caps.get("ru"))
        url = next((t["url"] for t in tracks or [] if t.get("ext") == "json3"), None)
        meta = {"title": info.get("title"), "channel": info.get("channel") or info.get("uploader"),
                "views": info.get("view_count"), "duration": info.get("duration")}
        if not url:
            return [], meta
        # тем же клиентом yt-dlp: с его заголовками, прокси и PO token в URL
        try:
            raw = ydl.urlopen(url).read()
        except Exception as e:
            msg = str(e)[:120]
            if "429" in msg:
                msg += ": YouTube не отдаёт субтитры IP этого сервера"
            raise SubtitlesUnavailable(msg)
    try:
        data = json.loads(raw)
    except ValueError:
        raise SubtitlesUnavailable("ответ не JSON")
    segs = []
    for ev in data.get("events") or []:
        text = "".join(s.get("utf8", "") for s in ev.get("segs") or []).replace("\n", " ").strip()
        if text:
            segs.append((int(ev.get("tStartMs", 0) / 1000), text))
    return segs, meta


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


def process(run, item: dict, force: bool = False) -> None:
    name = "letsplay-1"
    with pool.connection() as conn:
        old = conn.execute("SELECT updated_at FROM letsplays WHERE game_id = %s", (item["id"],)).fetchone()
    if not force and old and old["updated_at"] > datetime.now(old["updated_at"].tzinfo) - REFRESH_AFTER:
        run.log(name, f"«{item['title']}»: летсплей свежий, пропуск", stage="letsplay", slug=item["slug"])
        return
    video = find_video(item["title"])
    if not video:
        with pool.connection() as conn:  # старая нерелевантная ссылка не должна остаться
            conn.execute("DELETE FROM letsplays WHERE game_id = %s", (item["id"],))
        run.log(name, f"«{item['title']}»: ролика именно про эту игру не нашлось", stage="letsplay", slug=item["slug"])
        return
    meta = {"title": video.get("title"), "channel": video.get("channel") or video.get("uploader"),
            "views": video.get("view_count"), "duration": video.get("duration")}
    segs, subs_error = [], None
    if subtitles_paused():
        subs_error = "YouTube заблокировал IP сервера, субтитры на паузе"
    else:
        try:
            segs, meta = transcript(video["id"])
        except Exception as e:  # ссылку на найденный ролик сохраняем при любой ошибке субтитров
            subs_error = str(e).replace("ERROR: ", "")[:120]
            _remember_block(subs_error)
    text = condense(segs)
    result, tin, tout, llm_error = {"summary": None, "moments": []}, 0, 0, None
    if text and settings.llm_enabled:
        try:
            result, tin, tout = summarize(item["title"], text, meta.get("duration"))
        except Exception as e:  # ссылку на ролик сохраняем и без заключения
            llm_error = str(e)
    cost = (tin * settings.llm_price_in + tout * settings.llm_price_out) / 1e6
    with pool.connection() as conn:
        conn.execute(
            """INSERT INTO letsplays (game_id, video_id, title, channel, views, duration_sec, summary, moments, transcript_chars, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
               ON CONFLICT (game_id) DO UPDATE SET video_id = EXCLUDED.video_id, title = EXCLUDED.title, channel = EXCLUDED.channel,
                 views = EXCLUDED.views, duration_sec = EXCLUDED.duration_sec, summary = EXCLUDED.summary,
                 moments = EXCLUDED.moments, transcript_chars = EXCLUDED.transcript_chars, updated_at = now()""",
            (item["id"], video["id"], meta.get("title") or video.get("title"), meta.get("channel"),
             meta.get("views") or video.get("view_count"), meta.get("duration") or video.get("duration"),
             result["summary"], json.dumps(result["moments"], ensure_ascii=False), len(text)),
        )
    if tin:
        bump(run.id, tokens_in=tin, tokens_out=tout, cost_usd=cost)
    if subs_error:
        note = f"субтитры недоступны ({subs_error}), только ссылка"
    elif not text:
        note = "субтитров нет, только ссылка"
    elif not settings.llm_enabled:
        note = "LLM не настроен, только ссылка"
    elif llm_error:
        note = f"заключение не получено ({llm_error[:80]}), только ссылка"
    else:
        note = f"{len(result['moments'])} моментов, {tin} → {tout} токенов, ${cost:.4f}"
    views = f"{meta.get('views') or 0:,}".replace(",", " ")
    run.log(name, f"«{item['title']}»: {views} просмотров, {note}",
            stage="letsplay", slug=item["slug"], level="warn" if llm_error or subs_error else "info",
            data={"tokens_in": tin, "tokens_out": tout, "cost": cost, "title": item["title"]} if tin else None)
