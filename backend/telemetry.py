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


def _estimate_tokens_from_chars(chars: int) -> int:
    return max(1, int(round(max(chars, 0) / 4)))


def _default_price_per_1m(provider: str, model: str, direction: str) -> float:
    m = model.lower()
    p = provider.lower()
    if p == "ollama":
        return 0.0
    if "haiku" in m:
        return 1.0 if direction == "input" else 5.0
    if "sonnet" in m:
        return 3.0 if direction == "input" else 15.0
    if "gpt-4o-mini" in m:
        return 0.15 if direction == "input" else 0.60
    if "gemini-2.5-flash" in m or "gemini-1.5-flash" in m:
        return 0.30 if direction == "input" else 2.50
    if "qwen-vl-plus" in m or "qwen-plus" in m:
        return 0.40 if direction == "input" else 1.20
    return 0.0


def _effective_usage(row: dict[str, Any]) -> dict[str, Any]:
    input_tokens = int(row.get("input_tokens", 0) or 0)
    output_tokens = int(row.get("output_tokens", 0) or 0)
    cache_creation_tokens = int(row.get("cache_creation_input_tokens", 0) or 0)
    cache_read_tokens = int(row.get("cache_read_input_tokens", 0) or 0)
    total_tokens = int(row.get("total_tokens", 0) or 0)
    cost = float(row.get("cost_usd", 0) or 0)
    estimated = bool(row.get("cost_estimated"))

    if total_tokens <= 0:
        input_tokens = input_tokens or _estimate_tokens_from_chars(int(row.get("input_chars", 0) or 0))
        output_tokens = output_tokens or _estimate_tokens_from_chars(int(row.get("output_chars", 0) or 0))
        total_tokens = input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens
        estimated = True

    if cost <= 0 and total_tokens > 0:
        provider = str(row.get("effective_provider") or row.get("provider") or "unknown")
        model = str(row.get("effective_model") or row.get("model") or "unknown")
        input_price = _default_price_per_1m(provider, model, "input")
        output_price = _default_price_per_1m(provider, model, "output")
        billable_input = input_tokens + cache_creation_tokens + (cache_read_tokens * 0.10)
        cost = ((billable_input / 1_000_000) * input_price) + ((output_tokens / 1_000_000) * output_price)
        estimated = True

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cost_usd": cost,
        "cost_estimated": estimated,
    }


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
        usage_rows = [_effective_usage(r) for r in task_rows]
        total_cost = sum(float(r.get("cost_usd", 0) or 0) for r in usage_rows)
        total_tokens = sum(int(r.get("total_tokens", 0) or 0) for r in usage_rows)
        estimated_costs = sum(1 for r in usage_rows if r.get("cost_estimated"))
        task_summary[task] = {
            "calls": len(task_rows),
            "median_ms": round(statistics.median(durations), 1) if durations else 0,
            "p95_ms": round(_percentile(durations, 95), 1),
            "contract_failure_rate": round(contract_failures / len(task_rows), 4) if task_rows else 0,
            "error_rate": round(errors / len(task_rows), 4) if task_rows else 0,
            "fallback_rate": round(fallbacks / len(task_rows), 4) if task_rows else 0,
            "avg_input_chars": round(sum(int(r.get("input_chars", 0) or 0) for r in task_rows) / len(task_rows), 1),
            "avg_output_chars": round(sum(int(r.get("output_chars", 0) or 0) for r in task_rows) / len(task_rows), 1),
            "total_tokens": total_tokens,
            "total_cost_usd": round(total_cost, 6),
            "avg_cost_usd": round(total_cost / len(task_rows), 6) if task_rows else 0,
            "cost_per_token_usd": round(total_cost / total_tokens, 10) if total_tokens else 0,
            "estimated_cost_rate": round(estimated_costs / len(task_rows), 4) if task_rows else 0,
        }

    route_summary = {}
    for (task, provider, model), route_rows in sorted(by_route.items()):
        durations = [float(r.get("duration_ms", 0) or 0) for r in route_rows if r.get("duration_ms") is not None]
        contract_failures = sum(1 for r in route_rows if r.get("contract_ok") is False)
        errors = sum(1 for r in route_rows if r.get("error"))
        fallbacks = sum(1 for r in route_rows if r.get("fallback_used"))
        usage_rows = [_effective_usage(r) for r in route_rows]
        total_cost = sum(float(r.get("cost_usd", 0) or 0) for r in usage_rows)
        total_tokens = sum(int(r.get("total_tokens", 0) or 0) for r in usage_rows)
        estimated_costs = sum(1 for r in usage_rows if r.get("cost_estimated"))
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
            "total_tokens": total_tokens,
            "total_cost_usd": round(total_cost, 6),
            "avg_cost_usd": round(total_cost / len(route_rows), 6) if route_rows else 0,
            "cost_per_token_usd": round(total_cost / total_tokens, 10) if total_tokens else 0,
            "estimated_cost_rate": round(estimated_costs / len(route_rows), 4) if route_rows else 0,
        }

    usage_by_id = {id(r): _effective_usage(r) for r in rows}
    total_cost = sum(float(r.get("cost_usd", 0) or 0) for r in usage_by_id.values())
    total_tokens = sum(int(r.get("total_tokens", 0) or 0) for r in usage_by_id.values())
    recent_expensive = sorted(
        [{**r, **usage_by_id[id(r)]} for r in rows[-200:]],
        key=lambda r: float(r.get("cost_usd", 0) or 0),
        reverse=True,
    )[:20]

    return {
        "total_calls": len(rows),
        "total_tokens": total_tokens,
        "total_cost_usd": round(total_cost, 6),
        "cost_per_token_usd": round(total_cost / total_tokens, 10) if total_tokens else 0,
        "by_task": task_summary,
        "by_route": route_summary,
        "recent_expensive_calls": recent_expensive,
    }


def summarize_context_events(events: Iterable[dict[str, Any]] | None = None) -> dict[str, Any]:
    rows = list(events) if events is not None else read_context_events()
    extraction_rows = [r for r in rows if r.get("event_type") == "ingest_context"]
    curation_rows = [r for r in rows if r.get("event_type") == "ingest_curation"]
    latency_rows = [r for r in rows if r.get("event_type") == "ingest_latency"]
    store_image_latency_rows = [r for r in rows if r.get("event_type") == "store_image_latency"]
    chat_rows = [r for r in rows if r.get("event_type") == "chat_context"]

    low_coverage = [
        r for r in extraction_rows
        if float(r.get("source_coverage_ratio", 1) or 1) < 0.75
    ]
    dropped_chat = [
        r for r in chat_rows
        if int(r.get("retrieved_chunks_dropped", 0) or 0) > 0
    ]
    verdict_counts: dict[str, int] = {}
    shape_counts: dict[str, int] = {}
    diagram_planned = 0
    for row in curation_rows:
        verdict = str(row.get("source_verdict") or "unknown")
        shape = str(row.get("knowledge_shape") or "unknown")
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
        shape_counts[shape] = shape_counts.get(shape, 0) + 1
        if row.get("diagram_planned"):
            diagram_planned += 1

    latency_values = [float(r.get("total_ms", 0) or 0) for r in latency_rows]
    store_latency_values = [float(r.get("total_ms", 0) or 0) for r in store_image_latency_rows]
    stage_totals: dict[str, float] = defaultdict(float)
    stage_counts: dict[str, int] = defaultdict(int)
    for row in latency_rows:
        stage_ms = row.get("stage_ms") or {}
        if not isinstance(stage_ms, dict):
            continue
        for stage, ms in stage_ms.items():
            try:
                value = float(ms or 0)
            except (TypeError, ValueError):
                continue
            stage_totals[str(stage)] += value
            stage_counts[str(stage)] += 1

    slow_stages = [
        {
            "stage": stage,
            "avg_ms": round(total / max(stage_counts[stage], 1), 1),
            "calls": stage_counts[stage],
        }
        for stage, total in stage_totals.items()
    ]
    slow_stages.sort(key=lambda r: r["avg_ms"], reverse=True)

    return {
        "total_events": len(rows),
        "ingest_events": len(extraction_rows),
        "ingest_curation_events": len(curation_rows),
        "ingest_latency_events": len(latency_rows),
        "store_image_latency_events": len(store_image_latency_rows),
        "chat_events": len(chat_rows),
        "low_source_coverage_events": len(low_coverage),
        "chat_events_with_dropped_chunks": len(dropped_chat),
        "ingest_latency_median_ms": round(statistics.median(latency_values), 1) if latency_values else 0,
        "ingest_latency_p95_ms": round(_percentile(latency_values, 95), 1),
        "store_image_latency_median_ms": round(statistics.median(store_latency_values), 1) if store_latency_values else 0,
        "store_image_latency_p95_ms": round(_percentile(store_latency_values, 95), 1),
        "ingest_slow_stages": slow_stages[:8],
        "recent_ingest_latency": latency_rows[-10:],
        "recent_store_image_latency": store_image_latency_rows[-10:],
        "curation_verdict_counts": verdict_counts,
        "curation_shape_counts": shape_counts,
        "curation_diagram_planned": diagram_planned,
        "recent_low_coverage": low_coverage[-10:],
        "recent_dropped_chat_context": dropped_chat[-10:],
        "recent_ingest_curation": curation_rows[-10:],
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
            f"{row['avg_input_chars']} | {row['avg_output_chars']} | "
            f"{row.get('total_tokens', 0)} | ${row.get('total_cost_usd', 0):.6f} |"
        )

    route_rows = []
    for row in llm_summary["by_route"].values():
        route_rows.append(
            f"| `{row['task']}` | `{row['provider']}` | `{row['model']}` | {row['calls']} | "
            f"{row['median_ms']} | {row['contract_failure_rate']:.2%} | {row['fallback_rate']:.2%} | "
            f"{row.get('total_tokens', 0)} | ${row.get('total_cost_usd', 0):.6f} |"
        )

    expensive_lines = [
        f"- `{r.get('task', 'unknown')}` `{r.get('effective_provider', r.get('provider', 'unknown'))}`/"
        f"`{r.get('effective_model', r.get('model', 'unknown'))}` cost=${float(r.get('cost_usd', 0) or 0):.6f}; "
        f"tokens={r.get('total_tokens', 0)}; estimated={bool(r.get('cost_estimated'))}"
        for r in llm_summary.get("recent_expensive_calls", [])[:10]
    ]

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
    curation_lines = [
        f"- `{r.get('source_verdict', 'unknown')}` / `{r.get('knowledge_shape', 'unknown')}` "
        f"diagram={r.get('diagram_planned', False)} from {r.get('source_url', '')}"
        for r in context_summary["recent_ingest_curation"]
    ]
    latency_lines = [
        f"- {r.get('total_ms', 0)} ms total; stages={json.dumps(r.get('stage_ms', {}), sort_keys=True)}; "
        f"images={r.get('image_count', 0)}; source={r.get('source_type', 'unknown')}"
        for r in context_summary["recent_ingest_latency"]
    ]
    store_latency_lines = [
        f"- {r.get('total_ms', 0)} ms total; stages={json.dumps(r.get('stage_ms', {}), sort_keys=True)}; "
        f"images={r.get('image_count', 0)}"
        for r in context_summary["recent_store_image_latency"]
    ]
    slow_stage_lines = [
        f"- `{r['stage']}` avg {r['avg_ms']} ms over {r['calls']} runs"
        for r in context_summary["ingest_slow_stages"]
    ]

    content = f"""---
generated_at: {datetime.now().isoformat()}
total_llm_calls: {llm_summary["total_calls"]}
total_llm_tokens: {llm_summary.get("total_tokens", 0)}
total_llm_cost_usd: {llm_summary.get("total_cost_usd", 0)}
---

# Inference Engineering Report

## Task Summary

| Task | Calls | Median ms | P95 ms | Contract failures | Fallbacks | Avg input chars | Avg output chars | Tokens | Cost |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
{chr(10).join(task_rows) if task_rows else "| none | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | $0.000000 |"}

## Route Summary

| Task | Provider | Model | Calls | Median ms | Contract failures | Fallbacks | Tokens | Cost |
|---|---|---|---:|---:|---:|---:|---:|---:|
{chr(10).join(route_rows) if route_rows else "| none | none | none | 0 | 0 | 0 | 0 | 0 | $0.000000 |"}

## Recent Expensive Calls

{chr(10).join(expensive_lines) if expensive_lines else "- None"}

## Context Budget Signals

- Ingest context events: {context_summary["ingest_events"]}
- Ingest curation events: {context_summary["ingest_curation_events"]}
- Ingest latency events: {context_summary["ingest_latency_events"]}
- Ingest latency median / p95: {context_summary["ingest_latency_median_ms"]} ms / {context_summary["ingest_latency_p95_ms"]} ms
- Store-image latency events: {context_summary["store_image_latency_events"]}
- Store-image latency median / p95: {context_summary["store_image_latency_median_ms"]} ms / {context_summary["store_image_latency_p95_ms"]} ms
- Chat context events: {context_summary["chat_events"]}
- Low source coverage events: {context_summary["low_source_coverage_events"]}
- Chat events with dropped chunks: {context_summary["chat_events_with_dropped_chunks"]}
- Curation verdicts: {json.dumps(context_summary["curation_verdict_counts"], sort_keys=True)}
- Knowledge shapes: {json.dumps(context_summary["curation_shape_counts"], sort_keys=True)}
- Diagrams planned: {context_summary["curation_diagram_planned"]}

### Recent Low Source Coverage

{chr(10).join(low_coverage_lines) if low_coverage_lines else "- None"}

### Recent Dropped Chat Context

{chr(10).join(dropped_lines) if dropped_lines else "- None"}

### Recent Ingest Curation

{chr(10).join(curation_lines) if curation_lines else "- None"}

### Ingest Latency

Slow stages:

{chr(10).join(slow_stage_lines) if slow_stage_lines else "- None"}

Recent runs:

{chr(10).join(latency_lines) if latency_lines else "- None"}

Store-image runs:

{chr(10).join(store_latency_lines) if store_latency_lines else "- None"}
"""
    path.write_text(content, encoding="utf-8")
    return {
        "path": str(path),
        "summary": llm_summary,
        "context": context_summary,
    }
