import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import revision
from revision import PageInfo

DAY = date(2026, 9, 30)


def page(slug, age_days, body=1000, maturity=50, unread=10):
    return PageInfo(slug=slug, title=slug, folder="cs", created=DAY - timedelta(days=age_days),
                    body_chars=body, maturity=maturity, days_since_read=unread)


def vault():
    return ([page(f"new-{i}", i) for i in range(1, 4)]
            + [page(f"weeks-{i}", 10 + i) for i in range(5)]
            + [page(f"months-{i}", 80 + i) for i in range(10)])


def slugs(picks):
    return [p["page"].slug for p in picks]


def test_same_day_same_set_new_day_new_set():
    a = revision.pick_daily_set(DAY, vault(), [])
    b = revision.pick_daily_set(DAY, list(reversed(vault())), [])
    c = revision.pick_daily_set(DAY + timedelta(days=1), vault(), [])
    assert slugs(a) == slugs(b)            # input order doesn't matter
    assert slugs(a) != slugs(c)


def test_mixes_buckets_and_skips_stubs():
    pages = vault() + [page("stub", 2, body=100)]
    picks = revision.pick_daily_set(DAY, pages, [])
    assert len(picks) == 5
    assert "stub" not in slugs(picks)
    buckets = [p["bucket"] for p in picks]
    assert buckets[:2] == ["recent", "weeks"] and set(buckets[2:]) == {"months"}


def test_due_pages_come_first_and_recent_ratings_rest():
    log = [
        {"slug": "months-3", "day": "2026-09-29", "rating": "forgot", "next_due": "2026-09-30"},
        {"slug": "new-1", "day": "2026-09-29", "rating": "knew", "next_due": "2026-10-06"},
    ]
    picks = revision.pick_daily_set(DAY, vault(), log)
    assert (picks[0]["page"].slug, picks[0]["bucket"]) == ("months-3", "due")
    assert "new-1" not in slugs(picks)     # rated yesterday, not due


def test_next_due_intervals():
    assert revision.next_due("forgot", DAY, 14) == (DAY + timedelta(days=1), None)
    assert revision.next_due("shaky", DAY, 14) == (DAY + timedelta(days=3), None)
    assert revision.next_due("knew", DAY, None) == (DAY + timedelta(days=7), 7)
    assert revision.next_due("knew", DAY, 7) == (DAY + timedelta(days=14), 14)
    assert revision.next_due("knew", DAY, 40) == (DAY + timedelta(days=60), 60)


def test_topic_is_stable_for_the_day_and_excludes_todays_set():
    pages = vault()
    exclude = {"months-0", "months-1"}
    a = revision.topic_of_the_day(DAY, pages, exclude)
    b = revision.topic_of_the_day(DAY, pages, exclude)
    assert a == b and a["name"] not in exclude and a["why"]


def test_rating_log_round_trip_and_knew_interval_grows(monkeypatch, tmp_path):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    assert revision.append_rating("kv-cache", "knew", DAY) == DAY + timedelta(days=7)
    assert revision.append_rating("kv-cache", "knew", DAY + timedelta(days=7)) == DAY + timedelta(days=21)
    assert revision.ratings_on(DAY) == {"kv-cache": "knew"}


def test_failed_generation_is_not_retried_immediately(monkeypatch):
    calls = []
    monkeypatch.setattr(revision, "cached_questions", lambda slug: None)
    monkeypatch.setattr(revision, "questions_for", lambda slug: calls.append(slug) or (_ for _ in ()).throw(ValueError("bad json")))

    class SyncThread:
        def __init__(self, target, daemon):
            self.target = target
        def start(self):
            self.target()

    monkeypatch.setattr(revision.threading, "Thread", SyncThread)
    revision._failed_at.clear()
    revision.fill_bank_in_background(["kv-cache"])
    revision.fill_bank_in_background(["kv-cache"])   # e.g. the UI's next 3s poll
    assert calls == ["kv-cache"]


def test_rating_today_does_not_change_todays_set():
    before = revision.pick_daily_set(DAY, vault(), [])
    first = before[0]["page"].slug
    log = [{"slug": first, "day": DAY.isoformat(), "rating": "knew", "next_due": (DAY + timedelta(days=7)).isoformat()}]
    assert slugs(revision.pick_daily_set(DAY, vault(), log)) == slugs(before)
