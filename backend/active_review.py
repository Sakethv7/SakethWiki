"""
Active concept review queue.

This module ranks concepts that deserve attention using deterministic signals:
maturity, stale reads/updates, missing backlinks, thin content, and unresolved
conflict markers.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime
from pathlib import Path

import vault_reader

_DEFAULT_VAULT = "/Users/sakethv7/SakethVault"


def _vault() -> Path:
    return Path(os.environ.get("VAULT_PATH", _DEFAULT_VAULT))


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _last_read_map() -> dict[str, datetime]:
    reads_path = _vault() / "_wiki" / "meta" / "reads.jsonl"
    out: dict[str, datetime] = {}
    if not reads_path.exists():
        return out
    for line in reads_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            page = rec.get("page") or rec.get("concept")
            ts_raw = rec.get("timestamp") or rec.get("ts")
            ts = datetime.fromisoformat(ts_raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if page and (page not in out or ts > out[page]):
            out[page] = ts
    return out


def _conflict_count(content: str) -> int:
    patterns = [
        r"\[!warning\]",
        r"\bCONTRADICTION\b",
        r"\bcontradicts\b",
        r"last_evolution:\s*[\"']?contradicts",
    ]
    return sum(len(re.findall(pattern, content, flags=re.IGNORECASE)) for pattern in patterns)


def _score_page(page: dict, content: str, backlinks: list[str], last_read: datetime | None, today: date) -> dict:
    meta = vault_reader._parse_frontmatter(content)
    maturity = meta.get("understanding_maturity")
    try:
        maturity = int(maturity) if maturity not in (None, "") else None
    except (TypeError, ValueError):
        maturity = None

    entry_count = int(meta.get("entry_count", page.get("entry_count", 0)) or 0)
    word_count = int(page.get("word_count", len(content.split())) or 0)
    last_updated = _parse_date(meta.get("last_updated") or meta.get("date") or page.get("last_updated"))
    days_since_update = (today - last_updated).days if last_updated else None
    days_since_read = (today - last_read.date()).days if last_read else None
    conflicts = _conflict_count(content)

    score = 0
    reasons: list[str] = []
    signals = {
        "maturity": maturity,
        "backlinks": len(backlinks),
        "entry_count": entry_count,
        "word_count": word_count,
        "days_since_update": days_since_update,
        "days_since_read": days_since_read,
        "conflicts": conflicts,
    }

    if conflicts:
        score += min(40, conflicts * 20)
        reasons.append(f"{conflicts} unresolved conflict marker(s)")

    if maturity is None:
        score += 18
        reasons.append("missing maturity score")
    elif maturity < 40:
        score += 30
        reasons.append(f"low maturity ({maturity})")
    elif maturity < 70:
        score += 18
        reasons.append(f"medium-low maturity ({maturity})")

    if len(backlinks) == 0:
        score += 18
        reasons.append("no backlinks")
    elif len(backlinks) == 1:
        score += 8
        reasons.append("only one backlink")

    stale_basis = days_since_read if days_since_read is not None else days_since_update
    if stale_basis is None:
        score += 14
        reasons.append("no read/update date")
    elif stale_basis >= 120:
        score += 18
        reasons.append(f"stale for {stale_basis} days")
    elif stale_basis >= 45:
        score += 10
        reasons.append(f"not revisited in {stale_basis} days")

    if entry_count <= 1:
        score += 10
        reasons.append("single-entry concept")
    if word_count < 180:
        score += 8
        reasons.append("thin page")

    if not reasons:
        reasons.append("healthy; low review priority")

    if score >= 55:
        priority = "high"
    elif score >= 30:
        priority = "medium"
    else:
        priority = "low"

    return {
        "name": page["name"],
        "title": page.get("title", page["name"]),
        "folder": page.get("folder", "cs"),
        "priority": priority,
        "score": score,
        "reasons": reasons,
        "signals": signals,
        "suggested_action": _suggest_action(reasons, conflicts, maturity, len(backlinks)),
    }


def _suggest_action(reasons: list[str], conflicts: int, maturity: int | None, backlink_count: int) -> str:
    if conflicts:
        return "Resolve contradictions before adding more sources."
    if maturity is None:
        return "Calculate maturity, then decide whether the page needs synthesis."
    if backlink_count == 0:
        return "Add real backlinks or merge/delete if this is not a useful standalone concept."
    if maturity < 70:
        return "Review the current understanding block and add one clarifying source or synthesis."
    if any("stale" in reason or "revisited" in reason for reason in reasons):
        return "Re-read and confirm the current understanding is still accurate."
    return "No immediate action."


def build_queue(limit: int = 50, min_priority: str = "low") -> list[dict]:
    pages = vault_reader.list_concept_pages()
    backlinks = vault_reader.build_backlinks_index()
    last_reads = _last_read_map()
    today = date.today()

    rank = {"low": 0, "medium": 1, "high": 2}
    min_rank = rank.get(min_priority, 0)
    queue: list[dict] = []
    for page in pages:
        content = vault_reader.read_page(page["name"]) or ""
        item = _score_page(page, content, backlinks.get(page["name"], []), last_reads.get(page["name"]), today)
        if rank[item["priority"]] >= min_rank:
            queue.append(item)

    queue.sort(key=lambda item: (item["score"], item["name"]), reverse=True)
    return queue[: max(1, min(limit, 200))]
