"""Правила выбора игр за день и проверка цитат в резюме (без сети и БД)."""
from app.crawler import plan_see_all
from app.metacritic import score_or_none
from app.summarizer import verify_items

PAGES = {1: [f"a{i}" for i in range(24)], 2: [f"b{i}" for i in range(24)], 3: [f"c{i}" for i in range(24)]}
fetch = lambda p: PAGES.get(p, [])


def test_first_see_all_run_takes_20_and_keeps_cursor_on_unfinished_page():
    picked, page, last = plan_see_all(fetch, 1, set(), 20, 10)
    assert picked == PAGES[1][:20]
    assert page == 1 and last == 1  # 4 игры страницы ещё не обработаны


def test_next_run_finishes_page_and_moves_cursor():
    done = set(PAGES[1][:20])
    picked, page, last = plan_see_all(fetch, 1, done, 20, 10)
    assert picked == PAGES[1][20:] + PAGES[2][:16]
    assert page == 2 and last == 2


def test_already_processed_today_are_skipped():
    done = {"a0", "a5", "a7"}
    picked, _, _ = plan_see_all(fetch, 1, done, 20, 10)
    assert not done & set(picked)
    assert len(picked) == 20


def test_fully_processed_page_is_passed():
    picked, page, _ = plan_see_all(fetch, 1, set(PAGES[1]), 5, 10)
    assert picked == PAGES[2][:5] and page == 2


def test_stops_when_pages_end():
    picked, page, _ = plan_see_all(fetch, 3, set(), 30, 10)
    assert picked == PAGES[3] and page == 4


def test_tbd_score_is_none_not_zero():
    assert score_or_none(0) is None and score_or_none(None) is None and score_or_none(7.5) == 7.5


REVIEWS = [
    {"source": "Pub A", "quote": "The combat is fluid and every boss fight feels like a spectacular set piece."},
    {"source": "Player B", "quote": "Runs at 30 fps on my PC, stuttering everywhere, needs a patch."},
]


def test_verify_keeps_verbatim_quotes_and_takes_source_from_review():
    items = [{"text": "Отличный бой", "review": 1, "quote": "every boss fight feels like a spectacular set piece"}]
    ok, dropped = verify_items(items, REVIEWS)
    assert dropped == 0 and ok[0]["src"] == "Pub A"


def test_verify_drops_invented_quotes():
    items = [{"text": "Хороший сюжет", "review": 1, "quote": "the story is a masterpiece of the genre"}]
    ok, dropped = verify_items(items, REVIEWS)
    assert ok == [] and dropped == 1


def test_verify_finds_quote_when_review_index_is_wrong():
    items = [{"text": "Тормозит", "review": 1, "quote": "stuttering everywhere, needs a patch"}]
    ok, _ = verify_items(items, REVIEWS)
    assert ok[0]["src"] == "Player B"
