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
_VERSION = 1


def _vault() -> Path:
    return Path(os.environ.get("VAULT_PATH", _DEFAULT_VAULT))


def path() -> Path:
    return _vault() / "_wiki" / "meta" / "preferences.json"


def _empty() -> dict:
    return {
        "version": _VERSION,
        "updated_at": None,
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
    return base


def save(data: dict) -> dict:
    pref_path = path()
    pref_path.parent.mkdir(parents=True, exist_ok=True)
    data["version"] = _VERSION
    data["updated_at"] = datetime.now().isoformat()
    tmp = pref_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.rename(pref_path)
    return data


def _bump(mapping: dict, key: str, value: str, *, source: str = "") -> None:
    key = str(key or "").strip()
    value = str(value or "").strip()
    if not key or not value or key == value:
        return
    entry = mapping.setdefault(key, {})
    target = entry.setdefault(value, {"count": 0, "last_seen": None, "sources": []})
    target["count"] = int(target.get("count", 0)) + 1
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
            {"count": 0, "last_seen": None, "titles": []},
        )
        entry["count"] = int(entry.get("count", 0)) + 1
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
    if not options:
        return canonical
    best = max(options.items(), key=lambda kv: (int(kv[1].get("count", 0)), kv[0]))[0]
    return identity.resolve_slug(best)


def preferred_tags(tags: Iterable[str]) -> list[str]:
    data = load()
    corrections = data.get("tag_corrections", {})
    out: list[str] = []
    seen: set[str] = set()
    for tag in tags or []:
        tag = str(tag)
        options = corrections.get(tag, {})
        if options:
            tag = max(options.items(), key=lambda kv: (int(kv[1].get("count", 0)), kv[0]))[0]
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
            page_pairs.append((int(meta.get("count", 0)), src, dst))
    for count, src, dst in sorted(page_pairs, reverse=True)[:max_items]:
        lines.append(f"- Prefer page `{dst}` instead of `{src}` when the content matches that correction. Seen {count}x.")

    tag_pairs = []
    for src, targets in data.get("tag_corrections", {}).items():
        for dst, meta in targets.items():
            tag_pairs.append((int(meta.get("count", 0)), src, dst))
    for count, src, dst in sorted(tag_pairs, reverse=True)[:max_items]:
        lines.append(f"- Prefer tag `{dst}` instead of `{src}` when both could apply. Seen {count}x.")

    rejected = [
        (int(meta.get("count", 0)), slug)
        for slug, meta in data.get("rejected_pages", {}).items()
    ]
    for count, slug in sorted(rejected, reverse=True)[: max(3, max_items // 3)]:
        lines.append(f"- Be cautious about creating or using page `{slug}`; related suggestions were rejected {count}x.")

    for item in data.get("style", {}).get("summary", []):
        lines.append(f"- Summary style: {item}")

    return "\n".join(lines[: max_items + 8])


def chat_hints() -> str:
    data = load()
    return "\n".join(f"- {item}" for item in data.get("style", {}).get("chat", []))
