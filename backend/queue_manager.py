"""
Manages the HITL (Human-in-the-Loop) queue stored in hitl_queue.json.
Survives process restarts — pure file-based, no in-memory state.
"""
import json
import os
import tempfile
from pathlib import Path
from typing import Optional

QUEUE_PATH = Path(__file__).parent.parent / "hitl_queue.json"


def _read() -> list[dict]:
    if not QUEUE_PATH.exists():
        return []
    with open(QUEUE_PATH) as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def _write(items: list[dict]) -> None:
    """Atomic write: temp file + rename."""
    tmp = QUEUE_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(items, f, indent=2)
    tmp.rename(QUEUE_PATH)


def enqueue(item: dict) -> None:
    items = _read()
    items.append(item)
    _write(items)


def get_all() -> list[dict]:
    return _read()


def get_by_id(item_id: str) -> Optional[dict]:
    for item in _read():
        if item.get("id") == item_id:
            return item
    return None


def remove(item_id: str) -> bool:
    items = _read()
    new_items = [i for i in items if i.get("id") != item_id]
    if len(new_items) == len(items):
        return False
    _write(new_items)
    return True


def update(item_id: str, updated: dict) -> bool:
    """Replace a queue item in-place (used by background extraction)."""
    items = _read()
    for i, item in enumerate(items):
        if item.get("id") == item_id:
            items[i] = updated
            _write(items)
            return True
    return False
