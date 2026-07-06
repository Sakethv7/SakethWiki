"""
Tiny replay eval harness for SakethWiki system-loop gates.

This is intentionally small and deterministic first. It uses trace-derived
expected outcomes and local retrieval behavior before involving LLM judges.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import identity
import memory_store
import preference_memory
import telemetry

_DEFAULT_VAULT = "/Users/sakethv7/SakethVault"


def _vault() -> Path:
    return Path(os.environ.get("VAULT_PATH", _DEFAULT_VAULT))


def _meta_dir() -> Path:
    path = _vault() / "_wiki" / "meta"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _traces_path() -> Path:
    return _meta_dir() / "traces.jsonl"


def eval_exclusions_path() -> Path:
    return _meta_dir() / "eval-exclusions.json"


def eval_cases_path() -> Path:
    return _meta_dir() / "eval-cases.json"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def load_eval_exclusions() -> dict[str, Any]:
    path = eval_exclusions_path()
    if not path.exists():
        return {"updated_at": None, "excluded_cases": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"updated_at": None, "excluded_cases": {}}
    if not isinstance(data, dict):
        return {"updated_at": None, "excluded_cases": {}}
    data.setdefault("excluded_cases", {})
    return data


def save_eval_exclusions(data: dict[str, Any]) -> dict[str, Any]:
    data["updated_at"] = datetime.now().isoformat()
    path = eval_exclusions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.rename(path)
    return data


def exclude_eval_case(case_id: str, reason: str = "") -> dict[str, Any]:
    data = load_eval_exclusions()
    data.setdefault("excluded_cases", {})[case_id] = {
        "reason": reason,
        "excluded_at": datetime.now().isoformat(),
    }
    return save_eval_exclusions(data)


def load_curated_eval_cases() -> list[dict[str, Any]]:
    path = eval_cases_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    raw_cases = data.get("cases", data) if isinstance(data, dict) else data
    if not isinstance(raw_cases, list):
        return []
    excluded = set((load_eval_exclusions().get("excluded_cases") or {}).keys())
    cases: list[dict[str, Any]] = []
    for i, row in enumerate(raw_cases, start=1):
        if not isinstance(row, dict):
            continue
        case_id = str(row.get("id") or f"curated-{i}")
        if case_id in excluded:
            continue
        expected = identity.resolve_slug(row.get("expected_page") or row.get("page") or "")
        query = str(row.get("query") or row.get("title") or "").strip()
        if not expected or not query:
            continue
        cases.append({
            "id": case_id,
            "title": row.get("title", query),
            "source_type": row.get("source_type", "curated"),
            "suggested_page": row.get("suggested_page", ""),
            "expected_page": expected,
            "tags_suggested": row.get("tags_suggested", []),
            "tags_expected": row.get("tags_expected", row.get("tags", [])),
            "query": query,
            "curated": True,
        })
    return cases


def _trace_eval_cases(limit: int = 50) -> list[dict[str, Any]]:
    traces = _read_jsonl(_traces_path())
    excluded = set((load_eval_exclusions().get("excluded_cases") or {}).keys())
    cases: list[dict[str, Any]] = []
    for trace in traces:
        if not trace.get("approved"):
            continue
        final_page = str(trace.get("final_page") or "").strip()
        if not final_page:
            continue
        case_id = f"trace-{len(cases) + 1}"
        if case_id in excluded:
            continue
        cases.append(
            {
                "id": case_id,
                "title": trace.get("title", ""),
                "source_type": trace.get("source_type", ""),
                "suggested_page": trace.get("suggested_page", ""),
                "expected_page": identity.resolve_slug(final_page),
                "tags_suggested": trace.get("tags_suggested", []),
                "tags_expected": trace.get("tags_final", []),
                "query": trace.get("title", "") or final_page.replace("-", " "),
            }
        )
    curated = load_curated_eval_cases()
    combined = [*curated, *cases[-limit:]]
    return combined[-max(limit, len(curated)):]


def run_preference_replay_eval(limit: int = 50) -> dict[str, Any]:
    cases = _trace_eval_cases(limit=limit)
    page_total = 0
    page_pass = 0
    tag_total = 0
    tag_overlap_sum = 0.0
    failures: list[dict[str, Any]] = []

    for case in cases:
        expected_page = case["expected_page"]
        candidate_page = case.get("suggested_page") or expected_page
        predicted_page = preference_memory.preferred_page(candidate_page)
        if expected_page:
            page_total += 1
            if identity.resolve_slug(predicted_page) == identity.resolve_slug(expected_page):
                page_pass += 1
            else:
                failures.append(
                    {
                        "suite": "preference_replay",
                        "case_id": case["id"],
                        "expected": expected_page,
                        "got": predicted_page,
                        "title": case.get("title", ""),
                    }
                )

        expected_tags = {str(t) for t in case.get("tags_expected", []) if str(t).strip()}
        suggested_tags = [str(t) for t in case.get("tags_suggested", []) if str(t).strip()]
        if expected_tags and suggested_tags:
            tag_total += 1
            predicted_tags = set(preference_memory.preferred_tags(suggested_tags))
            tag_overlap_sum += len(predicted_tags & expected_tags) / max(1, len(expected_tags))

    return {
        "suite": "preference_replay",
        "cases": len(cases),
        "page_total": page_total,
        "page_pass": page_pass,
        "page_accuracy": round(page_pass / page_total, 4) if page_total else None,
        "tag_total": tag_total,
        "tag_overlap_avg": round(tag_overlap_sum / tag_total, 4) if tag_total else None,
        "failures": failures[:20],
    }


def run_retrieval_eval(limit: int = 30) -> dict[str, Any]:
    cases = _trace_eval_cases(limit=limit)
    total = 0
    top1 = 0
    top3 = 0
    failures: list[dict[str, Any]] = []
    try:
        memory_store.sync_index()
    except Exception as exc:
        return {
            "suite": "retrieval",
            "cases": 0,
            "top1": 0,
            "top3": 0,
            "top1_rate": None,
            "top3_rate": None,
            "failures": [{"suite": "retrieval", "error": f"sync failed: {exc}"}],
        }

    for case in cases:
        query = str(case.get("query") or "").strip()
        expected = identity.resolve_slug(case.get("expected_page", ""))
        if not query or not expected:
            continue
        total += 1
        try:
            hits = memory_store.search(query, limit=3, sync=False)
        except Exception as exc:
            failures.append(
                {
                    "suite": "retrieval",
                    "case_id": case["id"],
                    "expected": expected,
                    "got": [],
                    "error": str(exc),
                }
            )
            continue
        names = [identity.resolve_slug(hit.get("page_name", "")) for hit in hits]
        if names and names[0] == expected:
            top1 += 1
        if expected in names[:3]:
            top3 += 1
        else:
            failures.append(
                {
                    "suite": "retrieval",
                    "case_id": case["id"],
                    "expected": expected,
                    "got": names,
                    "query": query,
                }
            )

    return {
        "suite": "retrieval",
        "cases": total,
        "top1": top1,
        "top3": top3,
        "top1_rate": round(top1 / total, 4) if total else None,
        "top3_rate": round(top3 / total, 4) if total else None,
        "failures": failures[:20],
    }


def run_eval(candidate: dict[str, Any] | None = None) -> dict[str, Any]:
    preference = run_preference_replay_eval()
    retrieval = run_retrieval_eval()
    context = telemetry.summarize_context_events()

    candidate_result = None
    passed = True
    reasons: list[str] = []
    if candidate:
        action = candidate.get("action")
        if action == "increase_chat_context_budget":
            before = int((candidate.get("current_state") or {}).get("chat_context_budget") or 0)
            after = int((candidate.get("proposed_change") or {}).get("chat_context_budget") or 0)
            recent_events = context.get("recent_dropped_chat_context", [])
            recent_drops = context.get("chat_events_with_dropped_chunks", 0)
            retrieval_top3 = retrieval.get("top3_rate")
            replay = replay_chat_budget_change(recent_events, before, after)
            passed = (
                after > before >= 1000
                and recent_drops >= 5
                and after <= 16000
                and (retrieval_top3 is None or retrieval_top3 >= 0.85)
                and replay.get("projected_drop_rate", 1) < replay.get("current_drop_rate", 0)
            )
            reasons.append(
                f"chat budget {before} -> {after}; dropped-context events={recent_drops}; retrieval_top3={retrieval_top3}; projected_drop_rate={replay.get('projected_drop_rate')}"
            )
        elif action == "increase_ingest_source_budget":
            before = int((candidate.get("current_state") or {}).get("ingest_source_budget") or 0)
            after = int((candidate.get("proposed_change") or {}).get("ingest_source_budget") or 0)
            recent = context.get("recent_low_coverage", [])
            replay = replay_ingest_budget_change(recent, before, after)
            passed = (
                after > before >= 4000
                and context.get("low_source_coverage_events", 0) >= 5
                and after <= 80000
                and replay.get("projected_low_coverage_events", 999999) < replay.get("current_low_coverage_events", 0)
            )
            reasons.append(
                f"ingest budget {before} -> {after}; projected_low_coverage={replay.get('projected_low_coverage_events')}/{replay.get('events')}"
            )
        elif action == "add_alias":
            proposed = candidate.get("proposed_change") or {}
            alias = identity.slugify(proposed.get("alias", ""))
            canonical = identity.resolve_slug(proposed.get("canonical", ""))
            current = identity.resolve_slug(alias)
            passed = bool(alias and canonical and current in {alias, canonical})
            reasons.append(f"alias `{alias}` currently resolves to `{current}`; proposed canonical `{canonical}`")
        elif action == "exclude_eval_case":
            proposed = candidate.get("proposed_change") or {}
            case_id = str(proposed.get("case_id", "")).strip()
            passed = bool(case_id)
            reasons.append(f"exclude eval case `{case_id}` from replay set")
        elif action == "disable_runtime_route_override":
            proposed = candidate.get("proposed_change") or {}
            task = str(proposed.get("task", "")).strip().upper()
            passed = bool(task)
            reasons.append(f"disable runtime route override for `{task}`")
        elif action == "queue_page_review":
            proposed = candidate.get("proposed_change") or {}
            page = identity.resolve_slug(proposed.get("page", ""))
            passed = bool(page)
            reasons.append(f"queue page review for `{page}`")
        elif action == "create_consolidation_candidate":
            proposed = candidate.get("proposed_change") or {}
            source = identity.resolve_slug(proposed.get("source", ""))
            target = identity.resolve_slug(proposed.get("target", ""))
            passed = bool(source and target and source != target)
            reasons.append(f"create consolidation candidate `{source}` -> `{target}`")
        else:
            passed = False
            reasons.append(f"no eval gate defined for action={action}")
        candidate_result = {
            "candidate_id": candidate.get("id"),
            "action": action,
            "passed": passed,
            "reasons": reasons,
        }

    report = {
        "generated_at": datetime.now().isoformat(),
        "passed": passed,
        "candidate": candidate_result,
        "preference_replay": preference,
        "retrieval": retrieval,
        "context": context,
    }
    write_eval_report(report)
    return report


def replay_chat_budget_change(events: list[dict[str, Any]], before: int, after: int) -> dict[str, Any]:
    total = len(events)
    if not total:
        return {"events": 0, "current_drop_rate": 0, "projected_drop_rate": 0}
    current_dropped = 0
    projected_dropped = 0
    for event in events:
        dropped_chunks = int(event.get("retrieved_chunks_dropped", 0) or 0)
        dropped_chars = int(event.get("context_chars_dropped", 0) or 0)
        used_chars = int(event.get("context_chars_used", 0) or 0)
        current_dropped += 1 if dropped_chunks > 0 else 0
        extra_capacity = max(0, after - max(before, used_chars))
        projected_dropped += 1 if dropped_chunks > 0 and dropped_chars > extra_capacity else 0
    return {
        "events": total,
        "current_drop_rate": round(current_dropped / total, 4),
        "projected_drop_rate": round(projected_dropped / total, 4),
        "current_dropped_events": current_dropped,
        "projected_dropped_events": projected_dropped,
    }


def replay_ingest_budget_change(events: list[dict[str, Any]], before: int, after: int) -> dict[str, Any]:
    total = len(events)
    if not total:
        return {"events": 0, "current_low_coverage_events": 0, "projected_low_coverage_events": 0}
    current_low = 0
    projected_low = 0
    for event in events:
        total_chars = int(event.get("source_chars_total", 0) or 0)
        used_chars = int(event.get("source_chars_used", 0) or 0)
        if not total_chars:
            continue
        current_ratio = min(1.0, used_chars / total_chars)
        projected_ratio = min(1.0, max(after, used_chars) / total_chars)
        current_low += 1 if current_ratio < 0.75 else 0
        projected_low += 1 if projected_ratio < 0.75 else 0
    return {
        "events": total,
        "current_low_coverage_events": current_low,
        "projected_low_coverage_events": projected_low,
        "current_low_coverage_rate": round(current_low / total, 4),
        "projected_low_coverage_rate": round(projected_low / total, 4),
    }


def write_eval_report(report: dict[str, Any]) -> Path:
    today = datetime.now().strftime("%Y-%m-%d")
    path = telemetry.reports_dir() / f"eval-{today}.md"
    pref = report["preference_replay"]
    retrieval = report["retrieval"]
    candidate = report.get("candidate") or {}
    failures = [*pref.get("failures", []), *retrieval.get("failures", [])][:12]
    failure_lines = [
        f"- `{f.get('suite')}` expected `{f.get('expected')}`, got `{f.get('got')}` for {f.get('title') or f.get('query') or f.get('case_id')}"
        for f in failures
    ]
    candidate_lines = []
    if candidate:
        candidate_lines = [
            f"- Candidate: `{candidate.get('candidate_id')}`",
            f"- Action: `{candidate.get('action')}`",
            f"- Passed: `{candidate.get('passed')}`",
            *[f"- Reason: {reason}" for reason in candidate.get("reasons", [])],
        ]
    content = f"""---
generated_at: {report["generated_at"]}
passed: {str(report["passed"]).lower()}
---

# Eval Report

## Candidate Gate

{chr(10).join(candidate_lines) if candidate_lines else "- No candidate evaluated"}

## Preference Replay

- Cases: {pref["cases"]}
- Page accuracy: {pref["page_accuracy"]}
- Tag overlap average: {pref["tag_overlap_avg"]}

## Retrieval

- Cases: {retrieval["cases"]}
- Top 1: {retrieval["top1_rate"]}
- Top 3: {retrieval["top3_rate"]}

## Failures

{chr(10).join(failure_lines) if failure_lines else "- None"}
"""
    path.write_text(content, encoding="utf-8")
    return path
