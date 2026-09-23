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
import llm_client
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


def action_candidates_path() -> Path:
    return _meta_dir() / "system-action-candidates.json"


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


def _read_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


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
    # Ids are the trace's position among approved traces with a final_page,
    # so an excluded id keeps pointing at the same trace.
    position = 0
    for trace in traces:
        if not trace.get("approved"):
            continue
        final_page = str(trace.get("final_page") or "").strip()
        if not final_page:
            continue
        position += 1
        case_id = f"trace-{position}"
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


def run_ingest_curation_eval(limit: int = 100) -> dict[str, Any]:
    traces = _read_jsonl(_traces_path())[-limit:]
    total = len(traces)
    with_contract = 0
    source_only = 0
    rejected_with_reason = 0
    diagram_mismatches = 0
    shape_counts: dict[str, int] = {}
    failures: list[dict[str, Any]] = []

    for i, trace in enumerate(traces, start=1):
        verdict = str(trace.get("source_verdict") or "").strip()
        shape = str(trace.get("knowledge_shape") or "").strip()
        plan = trace.get("diagram_plan") if isinstance(trace.get("diagram_plan"), dict) else {}
        discarded = trace.get("discarded_context") if isinstance(trace.get("discarded_context"), list) else []
        has_contract = bool(verdict and shape and isinstance(plan, dict))
        with_contract += 1 if has_contract else 0
        if not has_contract:
            failures.append({
                "suite": "ingest_curation",
                "case_id": f"trace-{i}",
                "expected": "curation contract",
                "got": "missing",
                "title": trace.get("title", ""),
            })
        if verdict == "source_only":
            source_only += 1
            if not discarded:
                failures.append({
                    "suite": "ingest_curation",
                    "case_id": f"trace-{i}",
                    "expected": "discarded_context for source_only",
                    "got": "empty",
                    "title": trace.get("title", ""),
                })
        if trace.get("approved") is False and discarded:
            rejected_with_reason += 1
        if shape:
            shape_counts[shape] = shape_counts.get(shape, 0) + 1
        if plan.get("needed") is False and trace.get("diagram"):
            diagram_mismatches += 1
            failures.append({
                "suite": "ingest_curation",
                "case_id": f"trace-{i}",
                "expected": "no diagram",
                "got": "diagram present",
                "title": trace.get("title", ""),
            })

    return {
        "suite": "ingest_curation",
        "cases": total,
        "contract_coverage": round(with_contract / total, 4) if total else None,
        "source_only": source_only,
        "rejected_with_reason": rejected_with_reason,
        "diagram_mismatches": diagram_mismatches,
        "shape_counts": shape_counts,
        "failures": failures[:20],
    }


def run_ingest_curation_judge(limit: int = 8) -> dict[str, Any]:
    """Bounded LLM judge for whether curation kept transferable knowledge and dropped noise."""
    traces = [
        trace for trace in _read_jsonl(_traces_path())
        if trace.get("source_verdict") and trace.get("knowledge_shape")
    ][-limit:]
    if not traces:
        return {
            "suite": "ingest_curation_judge",
            "ran": False,
            "reason": "no curation traces with contract fields",
            "cases": 0,
            "pass_rate": None,
            "failures": [],
        }

    cases: list[dict[str, Any]] = []
    start = max(1, len(_read_jsonl(_traces_path())) - len(traces) + 1)
    for offset, trace in enumerate(traces):
        cases.append({
            "case_id": f"trace-{start + offset}",
            "approved": bool(trace.get("approved")),
            "title": trace.get("title", ""),
            "source_type": trace.get("source_type", ""),
            "source_verdict": trace.get("source_verdict", ""),
            "knowledge_shape": trace.get("knowledge_shape", ""),
            "summary": trace.get("summary", []),
            "key_concepts": trace.get("key_concepts", []),
            "educational_core": trace.get("educational_core", []),
            "discarded_context": trace.get("discarded_context", []),
            "diagram_plan": trace.get("diagram_plan", {}),
            "diagram_present": bool(trace.get("diagram")),
            "final_page": trace.get("final_page", ""),
        })

    prompt = f"""You are judging SakethWiki ingestion curation quality.

SakethWiki should keep durable, transferable knowledge and discard event chronology, social proof, hype context, sponsor copy, or biographical namedropping unless it defines the concept.

For each case, decide if curation passed.

Pass criteria:
- educational_core and summary contain reusable concepts, mechanisms, distinctions, failure modes, or implementation lessons.
- discarded_context names details that should not become durable notes when such details exist.
- source_verdict is appropriate: ingest for durable educational sources, source_only for sources mostly useful as provenance/context, reject for no durable value.
- knowledge_shape matches the dominant structure.
- diagram_plan is justified. Prefer no diagram over a generic or fake graph.

Return ONLY JSON:
{{
  "cases": [
    {{
      "case_id": "trace-1",
      "passed": true,
      "score": 0.0,
      "issues": ["short issue"],
      "suggested_prompt_hint": "one concrete prompt hint or empty string"
    }}
  ],
  "summary": "2 sentence summary"
}}

Cases:
{json.dumps(cases, ensure_ascii=False, indent=2)[:12000]}
"""
    try:
        raw = llm_client.complete(
            task="ingest_curation_judge",
            model=None,
            max_tokens=1800,
            messages=[{"role": "user", "content": prompt}],
            expect_json=True,
            required_json_keys=["cases", "summary"],
        )
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```", 1)[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        data = json.loads(raw)
    except Exception as exc:
        return {
            "suite": "ingest_curation_judge",
            "ran": False,
            "reason": str(exc),
            "cases": len(cases),
            "pass_rate": None,
            "failures": [],
        }

    judged = data.get("cases", []) if isinstance(data, dict) else []
    failures: list[dict[str, Any]] = []
    passed = 0
    total = 0
    for row in judged:
        if not isinstance(row, dict):
            continue
        total += 1
        ok = bool(row.get("passed"))
        passed += 1 if ok else 0
        if not ok:
            case_id = str(row.get("case_id") or "").strip()
            case = next((c for c in cases if c["case_id"] == case_id), {})
            failures.append({
                "suite": "ingest_curation_judge",
                "case_id": case_id,
                "expected": "high quality curation",
                "got": "; ".join(str(x) for x in row.get("issues", []) if str(x).strip())[:500] or "judge failed case",
                "title": case.get("title", ""),
                "final_page": case.get("final_page", ""),
                "suggested_prompt_hint": str(row.get("suggested_prompt_hint") or "").strip(),
                "score": row.get("score"),
            })

    return {
        "suite": "ingest_curation_judge",
        "ran": True,
        "reason": "",
        "cases": total,
        "pass_rate": round(passed / total, 4) if total else None,
        "summary": str(data.get("summary") or ""),
        "failures": failures[:20],
    }


def run_system_level_eval() -> dict[str, Any]:
    """Evaluate runtime/control-loop health, not concept-page quality."""
    traces = _read_jsonl(_traces_path())
    context = telemetry.summarize_context_events()
    llm = telemetry.summarize_llm_calls()
    actions = telemetry.read_system_actions(limit=200)
    candidates_data = _read_json_file(action_candidates_path(), {"candidates": []})
    candidates = [c for c in candidates_data.get("candidates", []) if isinstance(c, dict)] if isinstance(candidates_data, dict) else []
    pending_statuses = {"candidate", "needs_approval", "eval_ready", "eval_failed", "apply_failed"}
    pending = [c for c in candidates if c.get("status", "candidate") in pending_statuses]

    trace_types: dict[str, int] = {}
    approved = 0
    rejected = 0
    malformed_trace_events = 0
    for trace in traces:
        event_type = str(trace.get("event_type") or ("approval" if "approved" in trace else "unknown"))
        trace_types[event_type] = trace_types.get(event_type, 0) + 1
        if trace.get("approved") is True:
            approved += 1
            if not trace.get("final_page"):
                malformed_trace_events += 1
        elif trace.get("approved") is False:
            rejected += 1

    task_risks = []
    for task, row in (llm.get("by_task") or {}).items():
        error_rate = float(row.get("error_rate", 0) or 0)
        contract_failure_rate = float(row.get("contract_failure_rate", 0) or 0)
        fallback_rate = float(row.get("fallback_rate", 0) or 0)
        if error_rate or contract_failure_rate or fallback_rate >= 0.2:
            task_risks.append({
                "task": task,
                "calls": row.get("calls", 0),
                "error_rate": error_rate,
                "contract_failure_rate": contract_failure_rate,
                "fallback_rate": fallback_rate,
            })
    task_risks.sort(key=lambda r: (r["error_rate"] + r["contract_failure_rate"] + r["fallback_rate"]), reverse=True)

    gates = [
        {
            "name": "trace_events_present",
            "passed": len(traces) > 0,
            "value": len(traces),
            "threshold": "> 0",
            "level": "fail",
        },
        {
            "name": "trace_schema_integrity",
            "passed": malformed_trace_events == 0,
            "value": malformed_trace_events,
            "threshold": "0 malformed approval traces",
            "level": "fail",
        },
        {
            "name": "chat_context_visibility",
            "passed": int(context.get("chat_events", 0) or 0) > 0,
            "value": context.get("chat_events", 0),
            "threshold": "> 0 chat telemetry events",
            "level": "warn",
        },
        {
            "name": "dropped_chat_context",
            "passed": int(context.get("chat_events_with_dropped_chunks", 0) or 0) == 0,
            "value": context.get("chat_events_with_dropped_chunks", 0),
            "threshold": "0 dropped-context events",
            "level": "warn",
        },
        {
            "name": "low_ingest_source_coverage",
            "passed": int(context.get("low_source_coverage_events", 0) or 0) == 0,
            "value": context.get("low_source_coverage_events", 0),
            "threshold": "0 low-coverage ingest events",
            "level": "warn",
        },
        {
            "name": "llm_task_errors",
            "passed": len(task_risks) == 0,
            "value": len(task_risks),
            "threshold": "0 tasks with errors, contract failures, or high fallback",
            "level": "warn",
        },
        {
            "name": "pending_system_actions",
            "passed": len(pending) == 0,
            "value": len(pending),
            "threshold": "0 pending action candidates",
            "level": "warn",
        },
    ]
    hard_failures = [gate for gate in gates if gate["level"] == "fail" and not gate["passed"]]
    warnings = [gate for gate in gates if gate["level"] == "warn" and not gate["passed"]]

    return {
        "suite": "system_level",
        "passed": not hard_failures,
        "warning_count": len(warnings),
        "gates": gates,
        "trace_counts": {
            "total": len(traces),
            "approved": approved,
            "rejected": rejected,
            "by_event_type": trace_types,
        },
        "telemetry_counts": {
            "llm_calls": llm.get("total_calls", 0),
            "chat_events": context.get("chat_events", 0),
            "chat_note_events": context.get("chat_note_events", 0),
            "ingest_events": context.get("ingest_events", 0),
            "system_actions": len(actions),
            "pending_actions": len(pending),
        },
        "task_risks": task_risks[:10],
        "recent_system_actions": actions[-10:],
        "pending_actions": [
            {
                "id": c.get("id"),
                "action": c.get("action"),
                "risk": c.get("risk"),
                "status": c.get("status"),
                "reason": c.get("reason"),
            }
            for c in pending[:10]
        ],
    }


def run_eval(candidate: dict[str, Any] | None = None, include_judge: bool = False) -> dict[str, Any]:
    preference = run_preference_replay_eval()
    retrieval = run_retrieval_eval()
    curation = run_ingest_curation_eval()
    curation_judge = run_ingest_curation_judge() if include_judge else {
        "suite": "ingest_curation_judge",
        "ran": False,
        "reason": "not requested",
        "cases": 0,
        "pass_rate": None,
        "failures": [],
    }
    context = telemetry.summarize_context_events()
    system_level = run_system_level_eval()

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
        "ingest_curation": curation,
        "ingest_curation_judge": curation_judge,
        "system_level": system_level,
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
    curation = report.get("ingest_curation", {})
    curation_judge = report.get("ingest_curation_judge", {})
    system_level = report.get("system_level", {})
    candidate = report.get("candidate") or {}
    failures = [
        *pref.get("failures", []),
        *retrieval.get("failures", []),
        *curation.get("failures", []),
        *curation_judge.get("failures", []),
    ][:12]
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
    gate_lines = [
        f"- {'PASS' if gate.get('passed') else gate.get('level', 'warn').upper()} `{gate.get('name')}`: {gate.get('value')} (target: {gate.get('threshold')})"
        for gate in system_level.get("gates", [])
    ]
    task_risk_lines = [
        f"- `{risk.get('task')}` calls={risk.get('calls')} errors={risk.get('error_rate')} contracts={risk.get('contract_failure_rate')} fallbacks={risk.get('fallback_rate')}"
        for risk in system_level.get("task_risks", [])
    ]
    pending_action_lines = [
        f"- `{row.get('action')}` {row.get('status')} {row.get('risk')}: {row.get('reason')}"
        for row in system_level.get("pending_actions", [])
    ]
    content = f"""---
generated_at: {report["generated_at"]}
passed: {str(report["passed"]).lower()}
---

# Eval Report

## System-Level Eval

- Passed hard gates: {system_level.get("passed")}
- Warnings: {system_level.get("warning_count")}
- Trace counts: {json.dumps(system_level.get("trace_counts", {}), sort_keys=True)}
- Telemetry counts: {json.dumps(system_level.get("telemetry_counts", {}), sort_keys=True)}

### System Gates

{chr(10).join(gate_lines) if gate_lines else "- No system gates recorded"}

### Runtime Task Risks

{chr(10).join(task_risk_lines) if task_risk_lines else "- None"}

### Pending System Actions

{chr(10).join(pending_action_lines) if pending_action_lines else "- None"}

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

## Ingest Curation

- Cases: {curation.get("cases")}
- Contract coverage: {curation.get("contract_coverage")}
- Source-only traces: {curation.get("source_only")}
- Rejected with explicit reason: {curation.get("rejected_with_reason")}
- Diagram plan mismatches: {curation.get("diagram_mismatches")}
- Knowledge shapes: {json.dumps(curation.get("shape_counts", {}), sort_keys=True)}

## Ingest Curation Judge

- Ran: {curation_judge.get("ran")}
- Cases: {curation_judge.get("cases")}
- Pass rate: {curation_judge.get("pass_rate")}
- Reason: {curation_judge.get("reason", "")}
- Summary: {curation_judge.get("summary", "")}

## Failures

{chr(10).join(failure_lines) if failure_lines else "- None"}
"""
    path.write_text(content, encoding="utf-8")
    return path
