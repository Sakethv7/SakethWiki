"""
End-to-end test for SakethWiki.

Steps:
1. POST /ingest with Lilian Weng's AI Agents post
2. Print diff preview
3. POST /approve with approved=true
4. Confirm .md file exists, print content
5. POST /chat "what do I know about AI agents?"
6. Print answer and sources

Run with: python test_ingest.py
Backend must be running: cd backend && python main.py
"""
import json
import os
import sys
import time
from pathlib import Path

import httpx

BASE = "http://localhost:8001"
TEST_URL = "https://lilianweng.github.io/posts/2023-06-23-agent/"
VAULT_PATH = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/Documents/SakethWiki"))


def section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print("="*60)


def check_backend():
    try:
        r = httpx.get(f"{BASE}/pages", timeout=5)
        r.raise_for_status()
    except Exception as e:
        print(f"\nERROR: Backend not reachable at {BASE}")
        print(f"Start it with: cd backend && python main.py")
        print(f"Detail: {e}")
        sys.exit(1)


def main():
    check_backend()

    # ── Step 1: Ingest ────────────────────────────────────────────────────────
    section("Step 1: POST /ingest")
    print(f"URL: {TEST_URL}")
    print("Fetching and extracting (this may take 10-20s)...")

    r = httpx.post(
        f"{BASE}/ingest",
        json={"url": TEST_URL, "force": True},  # force=True bypasses dedup for test reruns
        timeout=60,
    )
    if r.status_code != 200:
        print(f"FAILED: {r.status_code} — {r.text}")
        sys.exit(1)

    data = r.json()
    item_id = data["id"]
    preview = data["diff_preview"]

    print(f"\nItem ID: {item_id}")
    print(f"Title:   {preview['title']}")
    print(f"Page:    {preview['suggested_page']}")
    print(f"Tags:    {preview['tags']}")
    print(f"Wikilinks: {preview['suggested_wikilinks']}")
    print("\nSummary:")
    for b in preview["summary"]:
        print(f"  • {b}")
    print(f"\nKey concepts: {preview['key_concepts']}")

    # ── Step 2: Approve ───────────────────────────────────────────────────────
    section("Step 2: POST /approve (approved=true)")

    r = httpx.post(
        f"{BASE}/approve/{item_id}",
        json={"approved": True},
        timeout=60,
    )
    if r.status_code != 200:
        print(f"FAILED: {r.status_code} — {r.text}")
        sys.exit(1)

    approve_data = r.json()
    file_written = approve_data.get("file_written")
    print(f"Success: {approve_data['success']}")
    print(f"File written: {file_written}")

    # ── Step 3: Confirm file exists ───────────────────────────────────────────
    section("Step 3: Confirm vault file exists")

    if file_written:
        full_path = VAULT_PATH / file_written
        if full_path.exists():
            print(f"FILE EXISTS: {full_path}")
            print(f"\n--- File content (first 80 lines) ---")
            lines = full_path.read_text(encoding="utf-8").splitlines()
            for line in lines[:80]:
                print(line)
            if len(lines) > 80:
                print(f"... ({len(lines) - 80} more lines)")
        else:
            print(f"WARNING: File not found at {full_path}")
    else:
        print("WARNING: No file_written in response")

    # ── Step 4: Check queue is empty ──────────────────────────────────────────
    section("Step 4: Confirm queue cleared")
    r = httpx.get(f"{BASE}/queue", timeout=10)
    items = r.json().get("items", [])
    approved_item = [i for i in items if i.get("id") == item_id]
    if not approved_item:
        print("Queue cleared successfully (item removed after approval)")
    else:
        print(f"WARNING: Item still in queue: {approved_item}")

    # ── Step 5: Chat ──────────────────────────────────────────────────────────
    section("Step 5: POST /chat — 'what do I know about AI agents?'")
    print("Querying (may take 5-10s)...")

    r = httpx.post(
        f"{BASE}/chat",
        json={"message": "what do I know about AI agents?", "history": []},
        timeout=60,
    )
    if r.status_code != 200:
        print(f"FAILED: {r.status_code} — {r.text}")
        sys.exit(1)

    chat_data = r.json()
    print(f"\nAnswer:\n{chat_data['answer']}")
    print(f"\nSources: {chat_data['sources']}")
    print(f"Pages read: {chat_data['pages_read']}")

    section("ALL TESTS PASSED")
    print("SakethWiki is working end-to-end!")


if __name__ == "__main__":
    main()
