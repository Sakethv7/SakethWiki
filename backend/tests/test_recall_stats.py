import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main

NOW = datetime(2026, 9, 28, 12, 0)


def test_counts_reads_in_period_with_both_key_spellings():
    reads = [
        {"ts": "2026-09-27T10:00:00", "concept": "rag"},
        {"ts": "2026-09-26T10:00:00", "concept": "rag"},
        {"timestamp": "2026-09-20T10:00:00", "page": "lora"},
        {"ts": "2026-07-01T10:00:00", "concept": "old-page"},   # outside 30d
        {"ts": "not-a-date", "concept": "bad"},
        {"ts": "2026-09-27T10:00:00", "concept": ""},
    ]
    stats = main._recall_stats(reads, [], now=NOW, period_days=30)
    assert stats["pages_read"] == 3
    assert stats["unique_pages_read"] == 2


def test_counts_chat_and_interview_questions_only():
    events = [
        {"event_type": "chat_context", "ts": "2026-09-27T09:00:00"},
        {"event_type": "chat_context", "ts": "2026-09-01T09:00:00"},
        {"event_type": "interview_context", "ts": "2026-09-25T09:00:00"},
        {"event_type": "ingest_context", "ts": "2026-09-27T09:00:00"},
        {"event_type": "chat_context", "ts": "2026-06-01T09:00:00"},  # outside 30d
    ]
    stats = main._recall_stats([], events, now=NOW, period_days=30)
    assert stats["chat_questions"] == 2
    assert stats["interview_questions"] == 1
    assert stats["questions_asked"] == 3


def test_empty_inputs_give_zeros():
    assert main._recall_stats([], [], now=NOW) == {
        "pages_read": 0,
        "unique_pages_read": 0,
        "questions_asked": 0,
        "chat_questions": 0,
        "interview_questions": 0,
        "revisions": 0,
    }


def test_until_bounds_the_window_for_trends():
    prev_end = datetime(2026, 8, 29, 12, 0)   # NOW - 30 days
    reads = [
        {"ts": "2026-09-27T10:00:00", "concept": "current-window"},
        {"ts": "2026-08-20T10:00:00", "concept": "previous-window"},
        {"ts": "2026-07-01T10:00:00", "concept": "too-old"},
    ]
    events = [
        {"event_type": "chat_context", "ts": "2026-09-27T09:00:00"},
        {"event_type": "chat_context", "ts": "2026-08-15T09:00:00"},
    ]
    prev = main._recall_stats(reads, events, now=prev_end, period_days=30, until=prev_end)
    assert prev["pages_read"] == 1
    assert prev["questions_asked"] == 1


def test_dashboard_stats_until_excludes_later_traces():
    prev_end = datetime(2026, 8, 29, 12, 0)
    traces = [
        {"ts": "2026-09-27T10:00:00", "approved": True, "final_page": "a"},
        {"ts": "2026-08-20T10:00:00", "approved": True, "final_page": "b"},
        {"ts": "2026-08-21T10:00:00", "approved": False},
    ]
    prev = main._dashboard_stats_from_traces(traces, now=prev_end, period_days=30, until=prev_end)
    assert prev["total_approved"] == 1
    assert prev["approval_rate"] == 0.5


def test_counts_revision_ratings_in_window():
    log = [
        {"ts": "2026-09-27T08:31:00", "slug": "kv-cache", "rating": "knew"},
        {"ts": "2026-09-27T08:32:00", "slug": "rag", "rating": "shaky"},
        {"ts": "2026-07-01T08:31:00", "slug": "old", "rating": "forgot"},   # outside 30d
    ]
    assert main._recall_stats([], [], now=NOW, revision_log=log)["revisions"] == 2
