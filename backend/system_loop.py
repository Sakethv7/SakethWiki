"""
Automated system-loop routing for SakethWiki.

This module turns runtime reports into bounded actions. It intentionally keeps
knowledge updates separate from system behavior updates.
"""
from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import preference_memory
import telemetry
import eval_harness
import identity

_DEFAULT_VAULT = "/Users/sakethv7/SakethVault"
_MIN_ROUTE_CALLS = int(os.environ.get("SYSTEM_LOOP_MIN_ROUTE_CALLS", "20"))
_MIN_ROUTE_FALLBACK_RATE = float(os.environ.get("SYSTEM_LOOP_MIN_ROUTE_FALLBACK_RATE", "0.20"))
_MIN_CONTEXT_DROP_EVENTS = int(os.environ.get("SYSTEM_LOOP_MIN_CONTEXT_DROP_EVENTS", "5"))
_MIN_LOW_COVERAGE_EVENTS = int(os.environ.get("SYSTEM_LOOP_MIN_LOW_COVERAGE_EVENTS", "5"))
_CRITICAL_TASKS = {
    "INGEST_EXTRACT",
    "ANALYZE_TRACES",
    "LINT_SCAN",
    "LINT_JSON_FIX",
    "CONSOLIDATE_PAGES",
    "KNOWLEDGE_GAPS",
    "EVOLUTION_CLASSIFY",
}


def _vault() -> Path:
    return Path(os.environ.get("VAULT_PATH", _DEFAULT_VAULT))


def _meta_dir() -> Path:
    path = _vault() / "_wiki" / "meta"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _traces_path() -> Path:
    return _meta_dir() / "traces.jsonl"


def runtime_overrides_path() -> Path:
    return _meta_dir() / "runtime-routing-overrides.json"


def runtime_settings_path() -> Path:
    return _meta_dir() / "runtime-system-settings.json"


def action_candidates_path() -> Path:
    return _meta_dir() / "system-action-candidates.json"


def review_requests_path() -> Path:
    return _meta_dir() / "system-review-requests.json"


def consolidation_requests_path() -> Path:
    return _meta_dir() / "system-consolidation-requests.json"


def aliases_path() -> Path:
    return _meta_dir() / "aliases.json"


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


def _write_json_file(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.rename(path)


def load_runtime_overrides() -> dict[str, Any]:
    path = runtime_overrides_path()
    if not path.exists():
        return {"enabled": True, "routes": {}, "updated_at": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"enabled": True, "routes": {}, "updated_at": None}
    if not isinstance(data, dict):
        return {"enabled": True, "routes": {}, "updated_at": None}
    data.setdefault("enabled", True)
    data.setdefault("routes", {})
    return data


def save_runtime_overrides(data: dict[str, Any]) -> dict[str, Any]:
    data["updated_at"] = datetime.now().isoformat()
    path = runtime_overrides_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.rename(path)
    return data


def load_runtime_settings() -> dict[str, Any]:
    path = runtime_settings_path()
    if not path.exists():
        return {"updated_at": None, "settings": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"updated_at": None, "settings": {}}
    if not isinstance(data, dict):
        return {"updated_at": None, "settings": {}}
    data.setdefault("settings", {})
    return data


def save_runtime_settings(data: dict[str, Any]) -> dict[str, Any]:
    data["updated_at"] = datetime.now().isoformat()
    path = runtime_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.rename(path)
    return data


def _empty_candidates() -> dict[str, Any]:
    return {"version": 1, "updated_at": None, "candidates": []}


def load_action_candidates() -> dict[str, Any]:
    path = action_candidates_path()
    if not path.exists():
        return _empty_candidates()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_candidates()
    if not isinstance(data, dict):
        return _empty_candidates()
    data.setdefault("version", 1)
    data.setdefault("candidates", [])
    return data


def save_action_candidates(data: dict[str, Any]) -> dict[str, Any]:
    data["updated_at"] = datetime.now().isoformat()
    path = action_candidates_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.rename(path)
    return data


def _candidate_key(candidate: dict[str, Any]) -> str:
    return str(candidate.get("dedupe_key") or f"{candidate.get('action')}:{candidate.get('target')}")


def upsert_action_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    data = load_action_candidates()
    candidates = [c for c in data.get("candidates", []) if isinstance(c, dict)]
    key = _candidate_key(candidate)
    now = datetime.now().isoformat()
    candidate = {
        "id": candidate.get("id") or f"act-{uuid4().hex[:12]}",
        "created_at": now,
        "updated_at": now,
        "status": "candidate",
        "risk": "medium",
        "requires_approval": candidate.get("risk") == "high",
        "requires_eval": candidate.get("risk") == "medium",
        **candidate,
        "dedupe_key": key,
    }

    for i, existing in enumerate(candidates):
        if _candidate_key(existing) == key and existing.get("status") in {"candidate", "needs_approval", "eval_ready", "eval_failed", "apply_failed"}:
            merged = {
                **existing,
                **candidate,
                "id": existing.get("id", candidate["id"]),
                "created_at": existing.get("created_at", candidate["created_at"]),
                "updated_at": now,
            }
            candidates[i] = merged
            data["candidates"] = candidates
            save_action_candidates(data)
            return merged

    candidates.append(candidate)
    data["candidates"] = candidates[-500:]
    save_action_candidates(data)
    return candidate


def list_action_candidates(status: str | None = None) -> list[dict[str, Any]]:
    candidates = [
        c for c in load_action_candidates().get("candidates", [])
        if isinstance(c, dict)
    ]
    if status:
        candidates = [c for c in candidates if c.get("status") == status]
    return sorted(candidates, key=lambda c: c.get("updated_at", ""), reverse=True)


def _update_candidate(candidate_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    data = load_action_candidates()
    candidates = [c for c in data.get("candidates", []) if isinstance(c, dict)]
    for i, candidate in enumerate(candidates):
        if candidate.get("id") == candidate_id:
            candidate = {**candidate, **updates, "updated_at": datetime.now().isoformat()}
            candidates[i] = candidate
            data["candidates"] = candidates
            save_action_candidates(data)
            return candidate
    raise KeyError("action candidate not found")


def _apply_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    action = candidate.get("action")
    proposed = candidate.get("proposed_change") or {}
    if action == "increase_chat_context_budget":
        settings = load_runtime_settings()
        value = int(proposed.get("chat_context_budget", 0) or 0)
        if value < 1000:
            raise ValueError("chat_context_budget must be >= 1000")
        settings.setdefault("settings", {})["chat_context_budget"] = value
        save_runtime_settings(settings)
        return {"applied_setting": "chat_context_budget", "value": value}

    if action == "set_runtime_route_override":
        task = str(proposed.get("task", "")).strip().upper()
        provider = str(proposed.get("provider", "")).strip().lower()
        if not task or not provider:
            raise ValueError("task and provider are required")
        overrides = load_runtime_overrides()
        overrides.setdefault("routes", {})[task] = {
            "provider": provider,
            "reason": candidate.get("reason", "approved action candidate"),
            "source": "approved_action_candidate",
            "updated_at": datetime.now().isoformat(),
        }
        save_runtime_overrides(overrides)
        return {"applied_route": task, "provider": provider}

    if action == "disable_runtime_route_override":
        task = str(proposed.get("task", "")).strip().upper()
        if not task:
            raise ValueError("task is required")
        overrides = load_runtime_overrides()
        removed = (overrides.get("routes") or {}).pop(task, None)
        save_runtime_overrides(overrides)
        return {"disabled_route": task, "previous": removed}

    if action == "increase_ingest_source_budget":
        settings = load_runtime_settings()
        value = int(proposed.get("ingest_source_budget", 0) or 0)
        if value < 4000 or value > 80000:
            raise ValueError("ingest_source_budget must be between 4000 and 80000")
        settings.setdefault("settings", {})["ingest_source_budget"] = value
        save_runtime_settings(settings)
        return {"applied_setting": "ingest_source_budget", "value": value}

    if action == "add_alias":
        alias = identity.slugify(proposed.get("alias", ""))
        canonical = identity.resolve_slug(proposed.get("canonical", ""))
        if not alias or not canonical:
            raise ValueError("alias and canonical are required")
        path = aliases_path()
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                raw = {}
        else:
            raw = {}
        if isinstance(raw, dict) and isinstance(raw.get("canonical"), dict):
            canonical_map = raw["canonical"]
        elif isinstance(raw, dict):
            canonical_map = raw
            raw = {"canonical": canonical_map}
        else:
            canonical_map = {}
            raw = {"canonical": canonical_map}
        values = canonical_map.get(canonical, [])
        if isinstance(values, str):
            values = [values]
        values = [str(v) for v in values if str(v).strip()]
        if alias not in [identity.slugify(v) for v in values]:
            values.append(alias)
        canonical_map[canonical] = values
        raw["canonical"] = canonical_map
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, indent=2, sort_keys=True), encoding="utf-8")
        tmp.rename(path)
        return {"applied_alias": alias, "canonical": canonical}

    if action == "exclude_eval_case":
        case_id = str(proposed.get("case_id", "")).strip()
        if not case_id:
            raise ValueError("case_id is required")
        eval_harness.exclude_eval_case(case_id, candidate.get("reason", "excluded from Operations"))
        return {"excluded_case": case_id}

    if action == "queue_page_review":
        page = identity.resolve_slug(proposed.get("page", ""))
        if not page:
            raise ValueError("page is required")
        data = _read_json_file(review_requests_path(), {"updated_at": None, "requests": []})
        requests = [r for r in data.get("requests", []) if isinstance(r, dict)]
        requests.append({
            "page": page,
            "reason": candidate.get("reason", ""),
            "source_candidate_id": candidate.get("id"),
            "created_at": datetime.now().isoformat(),
        })
        data["requests"] = requests[-500:]
        data["updated_at"] = datetime.now().isoformat()
        _write_json_file(review_requests_path(), data)
        return {"queued_page_review": page}

    if action == "create_consolidation_candidate":
        source = identity.resolve_slug(proposed.get("source", ""))
        target = identity.resolve_slug(proposed.get("target", ""))
        if not source or not target or source == target:
            raise ValueError("distinct source and target are required")
        data = _read_json_file(consolidation_requests_path(), {"updated_at": None, "requests": []})
        requests = [r for r in data.get("requests", []) if isinstance(r, dict)]
        requests.append({
            "source": source,
            "target": target,
            "reason": candidate.get("reason", ""),
            "source_candidate_id": candidate.get("id"),
            "created_at": datetime.now().isoformat(),
        })
        data["requests"] = requests[-500:]
        data["updated_at"] = datetime.now().isoformat()
        _write_json_file(consolidation_requests_path(), data)
        return {"consolidation_candidate": {"source": source, "target": target}}

    raise ValueError(f"no apply handler for action={action}")


def approve_action_candidate(candidate_id: str) -> dict[str, Any]:
    candidate = next((c for c in list_action_candidates() if c.get("id") == candidate_id), None)
    if not candidate:
        raise KeyError("action candidate not found")
    if candidate.get("requires_eval") and candidate.get("eval_status") != "passed":
        eval_result = eval_harness.run_eval(candidate)
        status = "eval_ready" if eval_result.get("passed") else "eval_failed"
        candidate = _update_candidate(
            candidate_id,
            {
                "status": status,
                "eval_status": "passed" if eval_result.get("passed") else "failed",
                "eval_result": eval_result.get("candidate"),
                "last_eval_at": datetime.now().isoformat(),
            },
        )
        if not eval_result.get("passed"):
            telemetry.log_system_action(
                {
                    "action": candidate.get("action"),
                    "candidate_id": candidate_id,
                    "risk": candidate.get("risk"),
                    "reason": "eval gate failed",
                    "applied": False,
                }
            )
            return candidate
    try:
        result = _apply_candidate(candidate)
        updated = _update_candidate(
            candidate_id,
            {
                "status": "applied",
                "approved_at": datetime.now().isoformat(),
                "apply_result": result,
            },
        )
        telemetry.log_system_action(
            {
                "action": candidate.get("action"),
                "candidate_id": candidate_id,
                "risk": candidate.get("risk"),
                "reason": candidate.get("reason", ""),
                "applied": True,
            }
        )
        return updated
    except Exception as exc:
        updated = _update_candidate(
            candidate_id,
            {
                "status": "apply_failed",
                "approved_at": datetime.now().isoformat(),
                "error": str(exc),
            },
        )
        telemetry.log_system_action(
            {
                "action": candidate.get("action"),
                "candidate_id": candidate_id,
                "risk": candidate.get("risk"),
                "reason": str(exc),
                "applied": False,
            }
        )
        return updated


def reject_action_candidate(candidate_id: str, reason: str = "") -> dict[str, Any]:
    updated = _update_candidate(
        candidate_id,
        {
            "status": "rejected",
            "rejected_at": datetime.now().isoformat(),
            "rejection_reason": reason,
        },
    )
    telemetry.log_system_action(
        {
            "action": updated.get("action"),
            "candidate_id": candidate_id,
            "risk": updated.get("risk"),
            "reason": reason or "rejected by user",
            "applied": False,
        }
    )
    return updated


def run_candidate_eval(candidate_id: str) -> dict[str, Any]:
    candidate = next((c for c in list_action_candidates() if c.get("id") == candidate_id), None)
    if not candidate:
        raise KeyError("action candidate not found")
    result = eval_harness.run_eval(candidate)
    updated = _update_candidate(
        candidate_id,
        {
            "status": "eval_ready" if result.get("passed") else "eval_failed",
            "eval_status": "passed" if result.get("passed") else "failed",
            "eval_result": result.get("candidate"),
            "last_eval_at": datetime.now().isoformat(),
        },
    )
    telemetry.log_system_action(
        {
            "action": candidate.get("action"),
            "candidate_id": candidate_id,
            "risk": candidate.get("risk"),
            "reason": "eval gate passed" if result.get("passed") else "eval gate failed",
            "applied": False,
        }
    )
    return {"candidate": updated, "eval": result}


def _trace_patterns(traces: list[dict[str, Any]]) -> dict[str, Any]:
    page_corrections = Counter()
    tag_corrections = Counter()
    rejected_pages = Counter()
    evolution = Counter()

    for trace in traces:
        if trace.get("evolution_type"):
            evolution[str(trace.get("evolution_type"))] += 1
        if trace.get("approved") and trace.get("page_corrected"):
            src = str(trace.get("suggested_page") or "").strip()
            dst = str(trace.get("final_page") or "").strip()
            if src and dst and src != dst:
                page_corrections[(src, dst)] += 1
        if trace.get("approved") and trace.get("tags_corrected"):
            suggested = [str(t) for t in trace.get("tags_suggested", []) if str(t).strip()]
            final = [str(t) for t in trace.get("tags_final", []) if str(t).strip()]
            for src in suggested:
                if src not in final:
                    for dst in final:
                        tag_corrections[(src, dst)] += 1
        if not trace.get("approved"):
            slug = str(trace.get("suggested_page") or trace.get("title") or "unknown").strip()
            if slug:
                rejected_pages[slug] += 1

    return {
        "page_corrections": page_corrections,
        "tag_corrections": tag_corrections,
        "rejected_pages": rejected_pages,
        "evolution": evolution,
    }


def _auto_apply_preference_candidates(actions: list[dict[str, Any]]) -> None:
    for item in preference_memory.review_candidates():
        # Reviewed candidates were intentionally held back by the user, so don't
        # override them here. The preference module already auto-activates fresh
        # repeated evidence when it is recorded.
        if int(item.get("count", 0) or 0) < 2:
            continue
        kind = item["type"]
        key = item["key"]
        value = item.get("value") or key
        try:
            preference_memory.set_preference_status(kind, key, value, "active")
            action = {
                "action": "activate_preference",
                "kind": kind,
                "key": key,
                "value": value,
                "reason": "repeated correction evidence reached system-loop threshold",
                "applied": True,
            }
        except Exception as exc:
            action = {
                "action": "activate_preference",
                "kind": kind,
                "key": key,
                "value": value,
                "reason": str(exc),
                "applied": False,
            }
        actions.append(action)
        telemetry.log_system_action(action)


def _route_model_improvements(actions: list[dict[str, Any]]) -> None:
    summary = telemetry.summarize_llm_calls()
    overrides = load_runtime_overrides()
    routes = overrides.setdefault("routes", {})
    changed = False

    for route in summary.get("by_route", {}).values():
        task = str(route.get("task", ""))
        task_key = "".join(c if c.isalnum() else "_" for c in task).upper()
        calls = int(route.get("calls", 0) or 0)
        fallback_rate = float(route.get("fallback_rate", 0) or 0)
        error_rate = float(route.get("error_rate", 0) or 0)
        contract_failure_rate = float(route.get("contract_failure_rate", 0) or 0)

        if calls < _MIN_ROUTE_CALLS:
            continue
        if task_key not in _CRITICAL_TASKS:
            continue
        if max(fallback_rate, error_rate, contract_failure_rate) < _MIN_ROUTE_FALLBACK_RATE:
            continue

        existing = routes.get(task_key, {})
        if existing.get("provider") == "anthropic":
            continue

        routes[task_key] = {
            "provider": "anthropic",
            "reason": (
                f"auto-routed by system loop after {calls} calls; "
                f"fallback={fallback_rate:.1%}, error={error_rate:.1%}, contract_failure={contract_failure_rate:.1%}"
            ),
            "source": "system_loop",
            "updated_at": datetime.now().isoformat(),
        }
        changed = True
        action = {
            "action": "set_runtime_route_override",
            "task": task_key,
            "provider": "anthropic",
            "reason": routes[task_key]["reason"],
            "applied": True,
        }
        actions.append(action)
        telemetry.log_system_action(action)

    if changed:
        save_runtime_overrides(overrides)


def _context_actions(actions: list[dict[str, Any]]) -> dict[str, Any]:
    summary = telemetry.summarize_context_events()
    if summary["low_source_coverage_events"] >= _MIN_LOW_COVERAGE_EVENTS:
        recent = summary.get("recent_low_coverage", [])
        current_budget = max(
            [int(r.get("source_chars_used", 0) or 0) for r in recent] + [12000]
        )
        proposed_budget = min(80000, max(current_budget + 4000, int(current_budget * 1.5)))
        candidate = upsert_action_candidate(
            {
                "risk": "medium",
                "action": "increase_ingest_source_budget",
                "target": "ingest_extract",
                "status": "candidate",
                "title": "Increase ingest source budget",
                "reason": f"{summary['low_source_coverage_events']} ingest events saw less than 75% source coverage",
                "current_state": {"ingest_source_budget": current_budget},
                "proposed_change": {"ingest_source_budget": proposed_budget},
                "evidence": {
                    "recent_low_coverage": summary.get("recent_low_coverage", []),
                },
                "requires_eval": True,
                "requires_approval": False,
            }
        )
        action = {
            "action": "flag_context_trimming",
            "reason": f"{summary['low_source_coverage_events']} ingest events saw less than 75% source coverage",
            "candidate_id": candidate["id"],
            "applied": False,
            "next": "run eval gate before increasing source window globally",
        }
        actions.append(action)
        telemetry.log_system_action(action)
    if summary["chat_events_with_dropped_chunks"] >= _MIN_CONTEXT_DROP_EVENTS:
        current_budget = int((load_runtime_settings().get("settings") or {}).get("chat_context_budget") or os.environ.get("RAG_CONTEXT_BUDGET", "6000"))
        proposed_budget = min(16000, max(current_budget + 2000, int(current_budget * 1.5)))
        candidate = upsert_action_candidate(
            {
                "risk": "medium",
                "action": "increase_chat_context_budget",
                "target": "chat_answer",
                "status": "candidate",
                "title": "Increase chat context budget",
                "reason": f"{summary['chat_events_with_dropped_chunks']} chat events dropped retrieved chunks",
                "proposed_change": {"chat_context_budget": proposed_budget},
                "current_state": {"chat_context_budget": current_budget},
                "evidence": {
                    "recent_dropped_chat_context": summary.get("recent_dropped_chat_context", []),
                },
                "requires_eval": True,
                "requires_approval": False,
            }
        )
        action = {
            "action": "flag_chat_context_drops",
            "reason": f"{summary['chat_events_with_dropped_chunks']} chat events dropped retrieved chunks",
            "candidate_id": candidate["id"],
            "applied": False,
            "next": "evaluate retrieval top-k, chunk size, and context budget before changing chat prompt",
        }
        actions.append(action)
        telemetry.log_system_action(action)
    elif summary["chat_events_with_dropped_chunks"]:
        action = {
            "action": "collect_more_chat_context_evidence",
            "reason": (
                f"{summary['chat_events_with_dropped_chunks']} dropped-context events observed; "
                f"need {_MIN_CONTEXT_DROP_EVENTS} before staging a budget change"
            ),
            "applied": False,
        }
        actions.append(action)
        telemetry.log_system_action(action)
    return summary


def route_eval_findings(report: dict[str, Any], actions: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    actions = actions if actions is not None else []
    retrieval = report.get("retrieval") or {}
    for failure in retrieval.get("failures", [])[:10]:
        expected = str(failure.get("expected") or "").strip()
        query = str(failure.get("query") or "").strip()
        case_id = str(failure.get("case_id") or "").strip()
        if not expected or not query:
            continue
        alias = identity.slugify(query)
        candidate = upsert_action_candidate(
            {
                "risk": "medium",
                "action": "add_alias",
                "target": expected,
                "status": "candidate",
                "title": f"Add retrieval alias for {expected}",
                "reason": f"Retrieval eval missed `{expected}` for query `{query}`",
                "proposed_change": {"alias": alias, "canonical": expected},
                "evidence": failure,
                "requires_eval": True,
                "requires_approval": False,
            }
        )
        action = {
            "action": "stage_add_alias_candidate",
            "candidate_id": candidate["id"],
            "reason": candidate["reason"],
            "applied": False,
        }
        actions.append(action)
        telemetry.log_system_action(action)
        if case_id:
            noisy = upsert_action_candidate(
                {
                    "risk": "medium",
                    "action": "exclude_eval_case",
                    "target": case_id,
                    "status": "candidate",
                    "title": f"Exclude noisy eval case {case_id}",
                    "reason": f"Retrieval eval case `{case_id}` may be noisy if query `{query}` should not map to `{expected}`",
                    "proposed_change": {"case_id": case_id},
                    "evidence": failure,
                    "requires_eval": True,
                    "requires_approval": False,
                }
            )
            noisy_action = {
                "action": "stage_exclude_eval_case_candidate",
                "candidate_id": noisy["id"],
                "reason": noisy["reason"],
                "applied": False,
            }
            actions.append(noisy_action)
            telemetry.log_system_action(noisy_action)
    return actions


def run_trace_critic(actions: list[dict[str, Any]] | None = None, limit: int = 40) -> dict[str, Any]:
    actions = actions if actions is not None else []
    traces = _read_jsonl(_traces_path())[-limit:]
    if len(traces) < 5:
        return {"ran": False, "reason": "need at least 5 traces", "actions": []}

    compact = []
    for idx, trace in enumerate(traces, start=max(1, len(traces) - limit + 1)):
        compact.append({
            "case_id": f"trace-{idx}",
            "approved": bool(trace.get("approved")),
            "title": trace.get("title", ""),
            "suggested_page": trace.get("suggested_page", ""),
            "final_page": trace.get("final_page", ""),
            "page_corrected": bool(trace.get("page_corrected")),
            "tags_suggested": trace.get("tags_suggested", []),
            "tags_final": trace.get("tags_final", []),
            "source_type": trace.get("source_type", ""),
        })

    prompt = f"""You are a bounded critic for SakethWiki runtime traces.

Classify trace evidence quality. Do not recommend direct code edits. Return JSON only.

Allowed candidate types:
- add_alias: when a query/title clearly refers to an existing final_page but retrieval/page routing missed it
- exclude_eval_case: when a trace is too ambiguous/noisy to use as an eval fixture
- queue_page_review: when a page appears contradictory, weak, or repeatedly corrected
- create_consolidation_candidate: when two page slugs look like duplicate concepts

Traces:
{json.dumps(compact, indent=2)}

Return:
{{
  "quality_summary": "short summary",
  "bad_trace_cases": [{{"case_id": "trace-1", "reason": "why noisy"}}],
  "good_eval_cases": [{{"case_id": "trace-2", "reason": "why stable"}}],
  "action_candidates": [
    {{"action": "add_alias", "risk": "medium", "title": "...", "reason": "...", "proposed_change": {{"alias": "...", "canonical": "..."}}}},
    {{"action": "queue_page_review", "risk": "high", "title": "...", "reason": "...", "proposed_change": {{"page": "..."}}}}
  ]
}}
"""
    try:
        import llm_client
        raw = llm_client.complete(
            task="analyze_traces",
            model=None,
            max_tokens=2200,
            messages=[{"role": "user", "content": prompt}],
            expect_json=True,
            required_json_keys=["quality_summary", "action_candidates"],
        ).strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        report = json.loads(raw)
    except Exception as exc:
        telemetry.log_system_action({
            "action": "trace_critic",
            "reason": f"trace critic failed: {exc}",
            "applied": False,
        })
        return {"ran": False, "reason": str(exc), "actions": []}

    staged: list[dict[str, Any]] = []
    allowed = {"add_alias", "exclude_eval_case", "queue_page_review", "create_consolidation_candidate"}
    for raw_candidate in report.get("action_candidates", [])[:12]:
        if not isinstance(raw_candidate, dict):
            continue
        action_name = str(raw_candidate.get("action", "")).strip()
        if action_name not in allowed:
            continue
        proposed = raw_candidate.get("proposed_change") or {}
        if not isinstance(proposed, dict):
            continue
        normalized_changes: list[dict[str, Any]] = []
        if action_name == "add_alias":
            alias_raw = str(proposed.get("alias", "")).strip()
            canonical_raw = str(proposed.get("canonical", "")).strip()
            if not alias_raw or not canonical_raw:
                continue
            alias = identity.slugify(alias_raw)
            canonical = identity.resolve_slug(canonical_raw)
            if not alias or not canonical or alias == "general":
                continue
            normalized_changes.append({"alias": alias, "canonical": canonical})
        elif action_name == "exclude_eval_case":
            case_id = str(proposed.get("case_id", "")).strip()
            if case_id:
                normalized_changes.append({"case_id": case_id})
            else:
                cases = proposed.get("cases", [])
                if isinstance(cases, list):
                    normalized_changes.extend(
                        {"case_id": str(case).strip()}
                        for case in cases[:5]
                        if str(case).strip()
                    )
        elif action_name == "queue_page_review":
            page_raw = str(proposed.get("page", "")).strip()
            page = identity.resolve_slug(page_raw) if page_raw else ""
            if page and page != "general":
                normalized_changes.append({"page": page})
            else:
                pages = proposed.get("pages", [])
                if isinstance(pages, list):
                    for raw_page in pages[:5]:
                        raw_page = str(raw_page).strip()
                        if not raw_page:
                            continue
                        page = identity.resolve_slug(raw_page)
                        if page and page != "general":
                            normalized_changes.append({"page": page})
        elif action_name == "create_consolidation_candidate":
            source_raw = str(proposed.get("source", "")).strip()
            target_raw = str(proposed.get("target", "")).strip()
            if not source_raw or not target_raw:
                continue
            source = identity.resolve_slug(source_raw)
            target = identity.resolve_slug(target_raw)
            if source and target and source != target:
                normalized_changes.append({"source": source, "target": target})
        if not normalized_changes:
            telemetry.log_system_action({
                "action": "drop_trace_critic_candidate",
                "reason": f"unsupported or incomplete schema for {action_name}",
                "applied": False,
            })
            continue

        risk = str(raw_candidate.get("risk") or ("high" if action_name in {"queue_page_review", "create_consolidation_candidate"} else "medium"))
        if action_name == "add_alias":
            risk = "medium"
        if action_name in {"queue_page_review", "create_consolidation_candidate"}:
            risk = "high"
        for normalized in normalized_changes:
            target = raw_candidate.get("target") or json.dumps(normalized, sort_keys=True)
            candidate = upsert_action_candidate({
                "risk": risk,
                "action": action_name,
                "target": target,
                "status": "needs_approval" if risk == "high" else "candidate",
                "title": raw_candidate.get("title") or action_name.replace("_", " ").title(),
                "reason": raw_candidate.get("reason", "staged by trace critic"),
                "proposed_change": normalized,
                "evidence": {"source": "trace_critic", "quality_summary": report.get("quality_summary")},
                "requires_eval": risk == "medium",
                "requires_approval": risk == "high",
            })
            event = {
                "action": "stage_trace_critic_candidate",
                "candidate_id": candidate["id"],
                "risk": risk,
                "reason": candidate.get("reason", ""),
                "applied": False,
            }
            actions.append(event)
            staged.append(candidate)
            telemetry.log_system_action(event)

    today = datetime.now().strftime("%Y-%m-%d")
    path = telemetry.reports_dir() / f"trace-critic-{today}.md"
    bad_lines = [
        f"- `{item.get('case_id')}`: {item.get('reason')}"
        for item in report.get("bad_trace_cases", [])
        if isinstance(item, dict)
    ]
    good_lines = [
        f"- `{item.get('case_id')}`: {item.get('reason')}"
        for item in report.get("good_eval_cases", [])
        if isinstance(item, dict)
    ]
    action_lines = [
        f"- `{item.get('action')}` ({item.get('risk', 'medium')}): {item.get('reason')}"
        for item in staged
    ]
    path.write_text(f"""---
generated_at: {datetime.now().isoformat()}
traces_seen: {len(traces)}
---

# Trace Critic Report

## Summary

{report.get("quality_summary", "")}

## Good Eval Cases

{chr(10).join(good_lines) if good_lines else "- None identified"}

## Noisy / Bad Trace Cases

{chr(10).join(bad_lines) if bad_lines else "- None identified"}

## Staged Action Candidates

{chr(10).join(action_lines) if action_lines else "- None staged"}
""", encoding="utf-8")
    return {"ran": True, "report_path": str(path), "staged": staged, "summary": report.get("quality_summary", "")}


def run_system_loop(auto_apply: bool = True) -> dict[str, Any]:
    traces = _read_jsonl(_traces_path())
    trace_patterns = _trace_patterns(traces)
    inference = telemetry.generate_inference_report()
    actions: list[dict[str, Any]] = []

    if auto_apply:
        _auto_apply_preference_candidates(actions)
        _route_model_improvements(actions)
    context_summary = _context_actions(actions)
    eval_report = eval_harness.run_eval()
    route_eval_findings(eval_report, actions)
    critic_report = run_trace_critic(actions)

    today = datetime.now().strftime("%Y-%m-%d")
    report_path = telemetry.reports_dir() / f"system-loop-{today}.md"

    page_lines = [
        f"- `{src}` -> `{dst}`: {count}x"
        for (src, dst), count in trace_patterns["page_corrections"].most_common(20)
    ]
    tag_lines = [
        f"- `{src}` -> `{dst}`: {count}x"
        for (src, dst), count in trace_patterns["tag_corrections"].most_common(20)
    ]
    reject_lines = [
        f"- `{slug}`: {count}x"
        for slug, count in trace_patterns["rejected_pages"].most_common(20)
    ]
    action_lines = [
        f"- {'applied' if a.get('applied') else 'queued'} `{a.get('action')}`: {a.get('reason', '')}"
        for a in actions
    ]

    content = f"""---
generated_at: {datetime.now().isoformat()}
traces_seen: {len(traces)}
auto_apply: {str(auto_apply).lower()}
---

# System Loop Report

## What This Report Controls

This report is about SakethWiki behavior, not concept-page knowledge. It routes improvements into preference memory, runtime routing overrides, and review/action logs.

```mermaid
flowchart TD
  A[Knowledge Loop] --> B[Concept page understanding improves]
  C[System Loop] --> D[Ingestion/retrieval/model behavior improves]
```

## Repeated Page Routing Corrections

{chr(10).join(page_lines) if page_lines else "- None observed"}

## Repeated Tag Corrections

{chr(10).join(tag_lines) if tag_lines else "- None observed"}

## Rejection Patterns

{chr(10).join(reject_lines) if reject_lines else "- None observed"}

## Context Budget Signals

- Low source coverage events: {context_summary["low_source_coverage_events"]}
- Chat events with dropped chunks: {context_summary["chat_events_with_dropped_chunks"]}

## Automated Actions

{chr(10).join(action_lines) if action_lines else "- No actions routed"}

## Inference Report

Latest inference report: `{Path(inference["path"]).name}`

## Trace Critic

{critic_report.get("summary") or critic_report.get("reason", "not run")}
"""
    report_path.write_text(content, encoding="utf-8")
    return {
        "success": True,
        "report_path": str(report_path),
        "inference_report_path": inference["path"],
        "actions": actions,
        "traces_seen": len(traces),
        "context": context_summary,
        "eval": eval_report,
        "trace_critic": critic_report,
    }
