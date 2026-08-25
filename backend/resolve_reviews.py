#!/usr/bin/env python3
"""
Resolve easy items in SakethWiki's review queues without a human clicking
through them one at a time.

Two domains, both dry-run by default:

  system  - Operations tab candidates (action_candidates.json). Low-risk
            candidates, and medium-risk candidates whose eval already
            passed, get approved via system_loop.approve_action_candidate().
            High-risk candidates are never touched.

  hitl    - The wiki queue (hitl_queue.json). Items the extraction step
            already scored source_verdict == "ingest" get approved via
            POST /queue/batch-decision on the running backend. Items scored
            "reject" or "source_only" are left for a human.

Design: docs/agent-review-resolution/{architecture,adr,logic_flow,api}.md

Usage:
    cd /Users/sakethv7/projects/Sakethwiki/backend
    source venv/bin/activate
    python resolve_reviews.py [--domain system|hitl|all] [--apply] [--limit N]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
import system_loop  # noqa: E402

BACKEND_URL = "http://127.0.0.1:8001"

TERMINAL_STATUSES = {"applied", "rejected", "apply_failed"}


def select_system_candidates() -> tuple[list[dict], list[dict]]:
    """Return (eligible, left_for_human) system-action candidates."""
    eligible, left = [], []
    for c in system_loop.list_action_candidates():
        if c.get("status") in TERMINAL_STATUSES:
            continue
        if c.get("requires_approval"):  # risk == "high"
            left.append(c)
        elif c.get("risk") == "low":
            eligible.append(c)
        elif c.get("risk") == "medium" and c.get("status") == "eval_ready":
            eligible.append(c)
        else:
            left.append(c)
    return eligible, left


def apply_system_candidates(eligible: list[dict], limit: int | None) -> list[dict]:
    results = []
    for c in eligible[:limit] if limit else eligible:
        try:
            updated = system_loop.approve_action_candidate(c["id"])
            results.append({"id": c["id"], "title": c.get("reason", c.get("action", "")),
                             "success": updated.get("status") == "applied",
                             "status": updated.get("status")})
        except Exception as exc:
            results.append({"id": c["id"], "title": c.get("reason", c.get("action", "")),
                             "success": False, "status": f"error: {exc}"})
    return results


def select_hitl_items(client: httpx.Client) -> tuple[list[dict], list[dict]]:
    """Return (eligible, left_for_human) HITL queue items."""
    resp = client.get("/queue")
    resp.raise_for_status()
    items = resp.json()
    items = items.get("items", items) if isinstance(items, dict) else items

    eligible, left = [], []
    for item in items:
        if item.get("pending_extraction") or item.get("extraction_error"):
            continue  # not ready — same gate /queue/batch-decision enforces
        if item.get("source_verdict") == "ingest":
            eligible.append(item)
        else:
            left.append(item)
    return eligible, left


def apply_hitl_items(client: httpx.Client, eligible: list[dict], limit: int | None) -> list[dict]:
    items = eligible[:limit] if limit else eligible
    results = []
    for batch_start in range(0, len(items), 100):
        batch = items[batch_start:batch_start + 100]
        ids = [i["id"] for i in batch]
        resp = client.post("/queue/batch-decision", json={"item_ids": ids, "approved": True})
        resp.raise_for_status()
        by_id = {r["item_id"]: r for r in resp.json().get("results", [])}
        for item in batch:
            r = by_id.get(item["id"], {})
            results.append({"id": item["id"], "title": item.get("title", ""),
                             "success": bool(r.get("success")),
                             "status": r.get("action") or r.get("code", "unknown")})
    return results


def print_system_section(eligible: list[dict], left: list[dict], applied: list[dict] | None) -> None:
    print("== System action candidates ==")
    if applied is None:
        for c in eligible:
            tier = "low risk" if c.get("risk") == "low" else "medium, eval passed"
            print(f"  eligible ({tier}): {c['id']} — {c.get('reason', c.get('action', ''))!r}")
        applied_count = 0
    else:
        for r in applied:
            tag = "applied" if r["success"] else f"failed: {r['status']}"
            print(f"  {tag}: {r['id']} — {r['title']!r}")
        applied_count = sum(1 for r in applied if r["success"])
    for c in left:
        reason = "high risk" if c.get("requires_approval") else f"risk={c.get('risk')}, status={c.get('status')}"
        print(f"  left for human ({reason}): {c['id']} — {c.get('reason', c.get('action', ''))!r}")
    dry = " (dry run)" if applied is None else ""
    print(f"  {len(eligible)} eligible, {applied_count} applied{dry}\n")


def print_hitl_section(eligible: list[dict], left: list[dict], applied: list[dict] | None) -> None:
    print("== HITL wiki queue ==")
    if applied is None:
        for item in eligible:
            print(f"  eligible-approve (source_verdict=ingest): {item['id']} — {item.get('title', '')!r}")
        applied_count = 0
    else:
        for r in applied:
            tag = "applied" if r["success"] else f"failed: {r['status']}"
            print(f"  {tag}: {r['id']} — {r['title']!r}")
        applied_count = sum(1 for r in applied if r["success"])
    for item in left:
        verdict = item.get("source_verdict", "missing")
        print(f"  left for human (source_verdict={verdict}): {item['id']} — {item.get('title', '')!r}")
    dry = " (dry run)" if applied is None else ""
    print(f"  {len(eligible)} eligible, {applied_count} applied{dry}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--domain", choices=["system", "hitl", "all"], default="all")
    parser.add_argument("--apply", action="store_true", help="Actually resolve eligible items (default: dry run)")
    parser.add_argument("--limit", type=int, default=None, help="Cap items applied in this run")
    args = parser.parse_args()

    if args.domain in ("system", "all"):
        eligible, left = select_system_candidates()
        applied = apply_system_candidates(eligible, args.limit) if args.apply else None
        print_system_section(eligible, left, applied)

    if args.domain in ("hitl", "all"):
        try:
            with httpx.Client(base_url=BACKEND_URL, timeout=30) as client:
                eligible, left = select_hitl_items(client)
                applied = apply_hitl_items(client, eligible, args.limit) if args.apply else None
                print_hitl_section(eligible, left, applied)
        except httpx.ConnectError:
            print(f"== HITL wiki queue ==\n  backend not reachable at {BACKEND_URL} — is SakethWiki running?\n")
            return 1

    if not args.apply:
        print("Run again with --apply to act on the above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
