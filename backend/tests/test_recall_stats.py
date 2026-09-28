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
    }
