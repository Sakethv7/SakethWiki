from pathlib import Path

import queue_manager

REAL_QUEUE = Path(queue_manager.__file__).resolve().parent.parent / "hitl_queue.json"
REAL_LOCK = REAL_QUEUE.with_suffix(".lock")


def _snapshot(path: Path):
    return path.read_bytes() if path.exists() else None


def test_enqueue_does_not_touch_real_queue(isolated_queue):
    before = (_snapshot(REAL_QUEUE), _snapshot(REAL_LOCK))

    queue_manager.enqueue({"id": "isolation-probe", "title": "should stay in tmp"})

    assert (_snapshot(REAL_QUEUE), _snapshot(REAL_LOCK)) == before
    assert queue_manager.QUEUE_PATH == isolated_queue
    assert isolated_queue.resolve() != REAL_QUEUE
    assert [i["id"] for i in queue_manager.get_all()] == ["isolation-probe"]
