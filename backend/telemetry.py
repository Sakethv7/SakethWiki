"""
Operational telemetry and report generation for SakethWiki.

These logs are product runtime evidence, not concept knowledge. They live under
_wiki/meta so reports can be inspected from the vault without contaminating
wiki pages.
"""
from __future__ import annotations

import json
import os
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

_DEFAULT_VAULT = "/Users/sakethv7/SakethVault"


def _vault() -> Path:
    return Path(os.environ.get("VAULT_PATH", _DEFAULT_VAULT))


def meta_dir() -> Path:
    path = _vault() / "_wiki" / "meta"
    path.mkdir(parents=True, exist_ok=True)
    return path


def reports_dir() -> Path:
    path = meta_dir() / "reports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def llm_log_path() -> Path:
    return meta_dir() / "llm_call_logs.jsonl"


def context_log_path() -> Path:
    return meta_dir() / "context_budget_logs.jsonl"


def action_log_path() -> Path:
    return meta_dir() / "system_actions.jsonl"


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def _read_jsonl(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    if limit is not None:
        lines = lines[-limit:]
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def estimate_chars(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    try:
        return len(json.dumps(value, ensure_ascii=False))
    except TypeError:
        return len(str(value))


def log_llm_call(record: dict[str, Any]) -> None:
    payload = {
        "ts": datetime.now().isoformat(),
        **record,
    }
    try:
        _append_jsonl(llm_log_path(), payload)
    except OSError:
        pass


def log_context_event(event_type: str, record: dict[str, Any]) -> None:
    payload = {
        "ts": datetime.now().isoformat(),
        "event_type": event_type,
        **record,
    }
    try:
        _append_jsonl(context_log_path(), payload)
    except OSError:
        pass


def log_system_action(record: dict[str, Any]) -> None:
    payload = {
        "ts": datetime.now().isoformat(),
        **record,
    }
    try:
        _append_jsonl(action_log_path(), payload)
    except OSError:
        pass


def read_llm_calls(limit: int | None = None) -> list[dict[str, Any]]:
    return _read_jsonl(llm_log_path(), limit=limit)


def read_context_events(limit: int | None = None) -> list[dict[str, Any]]:
    return _read_jsonl(context_log_path(), limit=limit)


def read_system_actions(limit: int | None = None) -> list[dict[str, Any]]:
    return _read_jsonl(action_log_path(), limit=limit)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    if len(values) == 1:
        return values[0]
    idx = int(round((pct / 100.0) * (len(values) - 1)))
    return values[max(0, min(idx, len(values) - 1))]


def summarize_llm_calls(calls: Iterable[dict[str, Any]] | None = None) -> dict[str, Any]:
    rows = list(calls) if calls is not None else read_llm_calls()
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_route: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        task = str(row.get("task", "unknown"))
        provider = str(row.get("provider", "unknown"))
        model = str(row.get("model", "unknown"))
        by_task[task].append(row)
        by_route[(task, provider, model)].append(row)

    task_summary = {}
    for task, task_rows in sorted(by_task.items()):
        durations = [float(r.get("duration_ms", 0) or 0) for r in task_rows if r.get("duration_ms") is not None]
        contract_failures = sum(1 for r in task_rows if r.get("contract_ok") is False)
        errors = sum(1 for r in task_rows if r.get("error"))
        fallbacks = sum(1 for r in task_rows if r.get("fallback_used"))
        task_summary[task] = {
            "calls": len(task_rows),
            "median_ms": round(statistics.median(durations), 1) if durations else 0,
            "p95_ms": round(_percentile(durations, 95), 1),
            "contract_failure_rate": round(contract_failures / len(task_rows), 4) if task_rows else 0,
            "error_rate": round(errors / len(task_rows), 4) if task_rows else 0,
            "fallback_rate": round(fallbacks / len(task_rows), 4) if task_rows else 0,
            "avg_input_chars": round(sum(int(r.get("input_chars", 0) or 0) for r in task_rows) / len(task_rows), 1),
            "avg_output_chars": round(sum(int(r.get("output_chars", 0) or 0) for r in task_rows) / len(task_rows), 1),
        }

    route_summary = {}
    for (task, provider, model), route_rows in sorted(by_route.items()):
        durations = [float(r.get("duration_ms", 0) or 0) for r in route_rows if r.get("duration_ms") is not None]
        contract_failures = sum(1 for r in route_rows if r.get("contract_ok") is False)
        errors = sum(1 for r in route_rows if r.get("error"))
        fallbacks = sum(1 for r in route_rows if r.get("fallback_used"))
        key = f"{task}::{provider}::{model}"
        route_summary[key] = {
            "task": task,
            "provider": provider,
            "model": model,
            "calls": len(route_rows),
            "median_ms": round(statistics.median(durations), 1) if durations else 0,
            "p95_ms": round(_percentile(durations, 95), 1),
            "contract_failure_rate": round(contract_failures / len(route_rows), 4) if route_rows else 0,
            "error_rate": round(errors / len(route_rows), 4) if route_rows else 0,
            "fallback_rate": round(fallbacks / len(route_rows), 4) if route_rows else 0,
        }

    return {
        "total_calls": len(rows),
        "by_task": task_summary,
        "by_route": route_summary,
    }


def summarize_context_events(events: Iterable[dict[str, Any]] | None = None) -> dict[str, Any]:
    rows = list(events) if events is not None else read_context_events()
    extraction_rows = [r for r in rows if r.get("event_type") == "ingest_context"]
    chat_rows = [r for r in rows if r.get("event_type") == "chat_context"]
    latency_rows = [r for r in rows if r.get("event_type") == "ingest_latency"]

    low_coverage = [
        r for r in extraction_rows
        if float(r.get("source_coverage_ratio", 1) or 1) < 0.75
    ]
    dropped_chat = [
        r for r in chat_rows
        if int(r.get("retrieved_chunks_dropped", 0) or 0) > 0
    ]
    latency_durations = [
        float(r.get("duration_ms", 0) or 0) for r in latency_rows if r.get("duration_ms") is not None
    ]
    latency_failures = sum(1 for r in latency_rows if r.get("success") is False)

    return {
        "total_events": len(rows),
        "ingest_events": len(extraction_rows),
        "chat_events": len(chat_rows),
        "low_source_coverage_events": len(low_coverage),
        "chat_events_with_dropped_chunks": len(dropped_chat),
        "recent_low_coverage": low_coverage[-10:],
        "recent_dropped_chat_context": dropped_chat[-10:],
        "ingest_latency_runs": len(latency_rows),
        "ingest_latency_median_ms": round(statistics.median(latency_durations), 1) if latency_durations else 0,
        "ingest_latency_p95_ms": round(_percentile(latency_durations, 95), 1) if latency_durations else 0,
        "ingest_latency_failures": latency_failures,
    }


def generate_inference_report() -> dict[str, Any]:
    llm_summary = summarize_llm_calls()
    context_summary = summarize_context_events()
    today = datetime.now().strftime("%Y-%m-%d")
    path = reports_dir() / f"inference-{today}.md"

    task_rows = []
    for task, row in llm_summary["by_task"].items():
        task_rows.append(
            f"| `{task}` | {row['calls']} | {row['median_ms']} | {row['p95_ms']} | "
            f"{row['contract_failure_rate']:.2%} | {row['fallback_rate']:.2%} | "
            f"{row['avg_input_chars']} | {row['avg_output_chars']} |"
        )

    route_rows = []
    for row in llm_summary["by_route"].values():
        route_rows.append(
            f"| `{row['task']}` | `{row['provider']}` | `{row['model']}` | {row['calls']} | "
            f"{row['median_ms']} | {row['contract_failure_rate']:.2%} | {row['fallback_rate']:.2%} |"
        )

    low_coverage_lines = [
        f"- `{r.get('task', 'ingest_extract')}` saw {float(r.get('source_coverage_ratio', 0) or 0):.1%} "
        f"of source ({r.get('source_chars_used', 0)} / {r.get('source_chars_total', 0)} chars) from {r.get('source_url', '')}"
        for r in context_summary["recent_low_coverage"]
    ]
    dropped_lines = [
        f"- chat used {r.get('retrieved_chunks_used', 0)} chunks and dropped "
        f"{r.get('retrieved_chunks_dropped', 0)} for query `{str(r.get('query', ''))[:90]}`"
        for r in context_summary["recent_dropped_chat_context"]
    ]

    content = f"""---
generated_at: {datetime.now().isoformat()}
total_llm_calls: {llm_summary["total_calls"]}
---

# Inference Engineering Report

## Task Summary

| Task | Calls | Median ms | P95 ms | Contract failures | Fallbacks | Avg input chars | Avg output chars |
|---|---:|---:|---:|---:|---:|---:|---:|
{chr(10).join(task_rows) if task_rows else "| none | 0 | 0 | 0 | 0 | 0 | 0 | 0 |"}

## Route Summary

| Task | Provider | Model | Calls | Median ms | Contract failures | Fallbacks |
|---|---|---|---:|---:|---:|---:|
{chr(10).join(route_rows) if route_rows else "| none | none | none | 0 | 0 | 0 | 0 |"}

## Context Budget Signals

- Ingest context events: {context_summary["ingest_events"]}
- Chat context events: {context_summary["chat_events"]}
- Low source coverage events: {context_summary["low_source_coverage_events"]}
- Chat events with dropped chunks: {context_summary["chat_events_with_dropped_chunks"]}

### Recent Low Source Coverage

{chr(10).join(low_coverage_lines) if low_coverage_lines else "- None"}

### Recent Dropped Chat Context

{chr(10).join(dropped_lines) if dropped_lines else "- None"}
"""
    path.write_text(content, encoding="utf-8")
    return {
        "path": str(path),
        "summary": llm_summary,
        "context": context_summary,
    }

