import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main

NOW = datetime(2026, 9, 28, 12, 0)


def write_insights(tmp_path, last_analyzed):
    path = tmp_path / "system-insights.md"
    path.write_text(f"---\nlast_analyzed: {last_analyzed}\n---\n\n# System Insights\n")
    return path


def test_due_when_week_passed_and_no_recent_attempt(tmp_path):
    insights = write_insights(tmp_path, "2026-09-08")
    assert main._analysis_due(insights, tmp_path / "traces.jsonl", None, NOW)


def test_not_due_within_a_week(tmp_path):
    insights = write_insights(tmp_path, "2026-09-25")
    assert not main._analysis_due(insights, tmp_path / "traces.jsonl", None, NOW)


def test_failed_attempt_backs_off_for_a_day(tmp_path):
    # last_analyzed stays stale when the analysis fails; the attempt time is
    # what stops the hourly retry loop.
    insights = write_insights(tmp_path, "2026-09-08")
    one_hour_ago = NOW - timedelta(hours=1)
    assert not main._analysis_due(insights, tmp_path / "traces.jsonl", one_hour_ago, NOW)


def test_retries_after_backoff_expires(tmp_path):
    insights = write_insights(tmp_path, "2026-09-08")
    yesterday = NOW - timedelta(days=1, minutes=1)
    assert main._analysis_due(insights, tmp_path / "traces.jsonl", yesterday, NOW)


def test_first_run_needs_five_traces(tmp_path):
    traces = tmp_path / "traces.jsonl"
    traces.write_text("{}\n" * 4)
    assert not main._analysis_due(tmp_path / "missing.md", traces, None, NOW)
    traces.write_text("{}\n" * 5)
    assert main._analysis_due(tmp_path / "missing.md", traces, None, NOW)


def test_compact_trace_drops_unused_fields():
    trace = {"approved": True, "final_page": "rag", "diagram": "graph TD; A-->B", "summary": ["x"]}
    assert main._compact_trace(trace) == {"approved": True, "final_page": "rag"}
