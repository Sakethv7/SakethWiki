"""
Durable user preference memory for SakethWiki.

This is intentionally separate from concept pages. Concept pages store knowledge;
this file stores repeated user correction patterns that should shape future
extraction, linking, and response style.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Iterable

import identity

_DEFAULT_VAULT = "/Users/sakethv7/SakethVault"
_VERSION = 2
_MIN_ACTIVE_EVIDENCE = 2
_VALID_STATUSES = {"candidate", "active", "rejected"}


def _vault() -> Path:
    return Path(os.environ.get("VAULT_PATH", _DEFAULT_VAULT))


def path() -> Path:
    return _vault() / "_wiki" / "meta" / "preferences.json"


def _empty() -> dict:
    return {
        "version": _VERSION,
        "updated_at": None,
        "min_active_evidence": _MIN_ACTIVE_EVIDENCE,
        "page_corrections": {},
        "tag_corrections": {},
        "rejected_pages": {},
        "style": {
            "summary": [
                "Use neutral, precise technical prose.",
                "Avoid first-person phrasing such as 'I learned'.",
                "Prefer mechanisms, constraints, and implications over generic summaries.",
            ],
            "chat": [
                "Start with the answer.",
                "Be direct and concrete.",
                "Call out mistakes when the question makes a wrong assumption.",
            ],
        },
        "events": [],
    }


def load() -> dict:
    pref_path = path()
    if not pref_path.exists():
        return _empty()
    try:
        data = json.loads(pref_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty()

    base = _empty()
    if not isinstance(data, dict):
        return base
    for key, value in data.items():
        if key == "style" and isinstance(value, dict):
            base["style"].update(value)
        else:
            base[key] = value
    base["version"] = _VERSION
    _normalize(base)
    return base


def _normalize(data: dict) -> None:
    data["min_active_evidence"] = _MIN_ACTIVE_EVIDENCE

    for corrections_key in ("page_corrections", "tag_corrections"):
        corrections = data.setdefault(corrections_key, {})
        if not isinstance(corrections, dict):
            data[corrections_key] = {}
            continue
        for targets in corrections.values():
            if not isinstance(targets, dict):
                continue
            for meta in targets.values():
                if not isinstance(meta, dict):
                    continue
                count = int(meta.get("count", 0) or 0)
                meta.setdefault("sources", [])
                meta["status"] = _status_for_count(
                    count,
                    str(meta.get("status", "")),
                    reviewed=bool(meta.get("reviewed_at")),
                )
                meta["confidence"] = _confidence(count, str(meta.get("status", "")))

    rejected = data.setdefault("rejected_pages", {})
    if not isinstance(rejected, dict):
        data["rejected_pages"] = {}
        return
    for meta in rejected.values():
        if not isinstance(meta, dict):
            continue
        count = int(meta.get("count", 0) or 0)
        meta.setdefault("titles", [])
        meta["status"] = _status_for_count(
            count,
            str(meta.get("status", "")),
            reviewed=bool(meta.get("reviewed_at")),
        )
        meta["confidence"] = _confidence(count, str(meta.get("status", "")))


def save(data: dict) -> dict:
    pref_path = path()
    pref_path.parent.mkdir(parents=True, exist_ok=True)
    data["version"] = _VERSION
    data["updated_at"] = datetime.now().isoformat()
    tmp = pref_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.rename(pref_path)
    return data


def _status_for_count(count: int, existing_status: str = "", *, reviewed: bool = False) -> str:
    if existing_status in {"active", "rejected"}:
        return existing_status
    if existing_status == "candidate" and reviewed:
        return existing_status
    return "active" if int(count or 0) >= _MIN_ACTIVE_EVIDENCE else "candidate"


def _confidence(count: int, status: str) -> float:
    if status == "rejected":
        return 0.0
    if status == "active":
        return min(0.95, 0.55 + (0.15 * int(count or 0)))
    return min(0.5, 0.2 + (0.1 * int(count or 0)))


def _stable(meta: dict) -> bool:
    if not isinstance(meta, dict):
        return False
    status = str(meta.get("status", "")).strip().lower()
    if status == "rejected":
        return False
    if status == "candidate" and meta.get("reviewed_at"):
        return False
    count = int(meta.get("count", 0) or 0)
    return status == "active" or count >= _MIN_ACTIVE_EVIDENCE


def _bump(mapping: dict, key: str, value: str, *, source: str = "") -> None:
    key = str(key or "").strip()
    value = str(value or "").strip()
    if not key or not value or key == value:
        return
    entry = mapping.setdefault(key, {})
    target = entry.setdefault(
        value,
        {
            "count": 0,
            "status": "candidate",
            "confidence": 0.0,
            "last_seen": None,
            "sources": [],
        },
    )
    target["count"] = int(target.get("count", 0)) + 1
    target["status"] = _status_for_count(
        target["count"],
        str(target.get("status", "")),
        reviewed=bool(target.get("reviewed_at")),
    )
    target["confidence"] = _confidence(target["count"], target["status"])
    target["last_seen"] = datetime.now().isoformat()
    if source and source not in target.setdefault("sources", []):
        target["sources"].append(source)
        target["sources"] = target["sources"][-5:]


def _record_event(data: dict, event: dict) -> None:
    events = data.setdefault("events", [])
    events.append({"ts": datetime.now().isoformat(), **event})
    data["events"] = events[-200:]


def record_approval_trace(trace: dict) -> dict:
    """
    Learn from an approve/reject trace. This uses only explicit user behavior:
    page corrections, tag corrections, and rejected suggestions.
    """
    data = load()
    source = trace.get("url", "") or trace.get("title", "") or "manual"

    suggested_page = identity.resolve_slug(trace.get("suggested_page", ""))
    final_page = identity.resolve_slug(trace.get("final_page", ""))
    if trace.get("approved") and trace.get("page_corrected"):
        _bump(data.setdefault("page_corrections", {}), suggested_page, final_page, source=source)

    if trace.get("approved") and trace.get("tags_corrected"):
        suggested_tags = [str(t) for t in trace.get("tags_suggested", []) if str(t).strip()]
        final_tags = [str(t) for t in trace.get("tags_final", []) if str(t).strip()]
        for suggested in suggested_tags:
            if suggested not in final_tags:
                for final in final_tags:
                    _bump(data.setdefault("tag_corrections", {}), suggested, final, source=source)

    if not trace.get("approved"):
        rejected = suggested_page or identity.slugify(trace.get("title", "rejected"))
        entry = data.setdefault("rejected_pages", {}).setdefault(
            rejected,
            {"count": 0, "status": "candidate", "confidence": 0.0, "last_seen": None, "titles": []},
        )
        entry["count"] = int(entry.get("count", 0)) + 1
        entry["status"] = _status_for_count(
            entry["count"],
            str(entry.get("status", "")),
            reviewed=bool(entry.get("reviewed_at")),
        )
        entry["confidence"] = _confidence(entry["count"], entry["status"])
        entry["last_seen"] = datetime.now().isoformat()
        title = trace.get("title", "")
        if title and title not in entry.setdefault("titles", []):
            entry["titles"].append(title)
            entry["titles"] = entry["titles"][-5:]

    _record_event(
        data,
        {
            "approved": bool(trace.get("approved")),
            "suggested_page": trace.get("suggested_page", ""),
            "final_page": trace.get("final_page", ""),
            "page_corrected": bool(trace.get("page_corrected")),
            "tags_corrected": bool(trace.get("tags_corrected")),
        },
    )
    return save(data)


def preferred_page(slug: str) -> str:
    canonical = identity.resolve_slug(slug)
    corrections = load().get("page_corrections", {})
    options = corrections.get(canonical, {})
    stable_options = {dst: meta for dst, meta in options.items() if _stable(meta)}
    if not stable_options:
        return canonical
    best = max(
        stable_options.items(),
        key=lambda kv: (float(kv[1].get("confidence", 0)), int(kv[1].get("count", 0)), kv[0]),
    )[0]
    return identity.resolve_slug(best)


def preferred_tags(tags: Iterable[str]) -> list[str]:
    data = load()
    corrections = data.get("tag_corrections", {})
    out: list[str] = []
    seen: set[str] = set()
    for tag in tags or []:
        tag = str(tag)
        options = corrections.get(tag, {})
        stable_options = {dst: meta for dst, meta in options.items() if _stable(meta)}
        if stable_options:
            tag = max(
                stable_options.items(),
                key=lambda kv: (float(kv[1].get("confidence", 0)), int(kv[1].get("count", 0)), kv[0]),
            )[0]
        if tag and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out


def prompt_hints(max_items: int = 12) -> str:
    data = load()
    lines: list[str] = []

    page_pairs = []
    for src, targets in data.get("page_corrections", {}).items():
        for dst, meta in targets.items():
            if _stable(meta):
                page_pairs.append((float(meta.get("confidence", 0)), int(meta.get("count", 0)), src, dst))
    for confidence, count, src, dst in sorted(page_pairs, reverse=True)[:max_items]:
        lines.append(f"- Prefer page `{dst}` instead of `{src}` when the content matches that correction. Seen {count}x; confidence {confidence:.2f}.")

    tag_pairs = []
    for src, targets in data.get("tag_corrections", {}).items():
        for dst, meta in targets.items():
            if _stable(meta):
                tag_pairs.append((float(meta.get("confidence", 0)), int(meta.get("count", 0)), src, dst))
    for confidence, count, src, dst in sorted(tag_pairs, reverse=True)[:max_items]:
        lines.append(f"- Prefer tag `{dst}` instead of `{src}` when both could apply. Seen {count}x; confidence {confidence:.2f}.")

    rejected = [
        (float(meta.get("confidence", 0)), int(meta.get("count", 0)), slug)
        for slug, meta in data.get("rejected_pages", {}).items()
        if _stable(meta)
    ]
    for confidence, count, slug in sorted(rejected, reverse=True)[: max(3, max_items // 3)]:
        lines.append(f"- Be cautious about creating or using page `{slug}`; related suggestions were rejected {count}x; confidence {confidence:.2f}.")

    for item in data.get("style", {}).get("summary", []):
        lines.append(f"- Summary style: {item}")

    return "\n".join(lines[: max_items + 8])


def chat_hints() -> str:
    data = load()
    return "\n".join(f"- {item}" for item in data.get("style", {}).get("chat", []))


def review_candidates() -> list[dict]:
    data = load()
    candidates: list[dict] = []

    for src, targets in data.get("page_corrections", {}).items():
        for dst, meta in targets.items():
            status = str(meta.get("status", "candidate"))
            if status == "candidate":
                candidates.append({
                    "type": "page_correction",
                    "key": src,
                    "value": dst,
                    "count": int(meta.get("count", 0)),
                    "confidence": float(meta.get("confidence", 0)),
                    "sources": meta.get("sources", []),
                })

    for src, targets in data.get("tag_corrections", {}).items():
        for dst, meta in targets.items():
            status = str(meta.get("status", "candidate"))
            if status == "candidate":
                candidates.append({
                    "type": "tag_correction",
                    "key": src,
                    "value": dst,
                    "count": int(meta.get("count", 0)),
                    "confidence": float(meta.get("confidence", 0)),
                    "sources": meta.get("sources", []),
                })

    for slug, meta in data.get("rejected_pages", {}).items():
        status = str(meta.get("status", "candidate"))
        if status == "candidate":
            candidates.append({
                "type": "rejected_page",
                "key": slug,
                "value": slug,
                "count": int(meta.get("count", 0)),
                "confidence": float(meta.get("confidence", 0)),
                "titles": meta.get("titles", []),
            })

    return sorted(candidates, key=lambda item: (item["count"], item["type"], item["key"]), reverse=True)


def set_preference_status(kind: str, key: str, value: str, status: str) -> dict:
    status = str(status or "").strip().lower()
    if status not in _VALID_STATUSES:
        raise ValueError(f"status must be one of {sorted(_VALID_STATUSES)}")

    data = load()
    key = str(key or "").strip()
    value = str(value or key or "").strip()
    if not key:
        raise ValueError("key is required")

    if kind == "page_correction":
        meta = data.get("page_corrections", {}).get(key, {}).get(value)
    elif kind == "tag_correction":
        meta = data.get("tag_corrections", {}).get(key, {}).get(value)
    elif kind == "rejected_page":
        meta = data.get("rejected_pages", {}).get(key)
    else:
        raise ValueError("kind must be page_correction, tag_correction, or rejected_page")

    if not isinstance(meta, dict):
        raise KeyError("preference candidate not found")

    count = int(meta.get("count", 0))
    meta["status"] = status
    meta["confidence"] = _confidence(count, status)
    meta["reviewed_at"] = datetime.now().isoformat()
    return save(data)
