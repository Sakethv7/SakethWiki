"""
Manages the HITL (Human-in-the-Loop) queue stored in hitl_queue.json.
Survives process restarts — pure file-based, no in-memory state.

Concurrency: all mutations hold an exclusive flock on a lock file so that
concurrent approvals (two browser tabs, iOS Shortcut + web UI) cannot
race on hitl_queue.json.
"""
import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

QUEUE_PATH = Path(__file__).parent.parent / "hitl_queue.json"
_LOCK_PATH = QUEUE_PATH.with_suffix(".lock")


@contextmanager
def _queue_lock():
    """Acquire an exclusive file lock for the duration of a read-modify-write."""
    _LOCK_PATH.touch(exist_ok=True)
    with open(_LOCK_PATH, "r") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


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
    with _queue_lock():
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
    with _queue_lock():
        items = _read()
        new_items = [i for i in items if i.get("id") != item_id]
        if len(new_items) == len(items):
            return False
        _write(new_items)
        return True


def update(item_id: str, updated: dict) -> bool:
    """Replace a queue item in-place (used by background extraction)."""
    with _queue_lock():
        items = _read()
        for i, item in enumerate(items):
            if item.get("id") == item_id:
                items[i] = updated
                _write(items)
                return True
    return False
