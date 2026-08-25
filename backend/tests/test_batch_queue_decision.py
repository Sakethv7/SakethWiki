import asyncio
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


def run_batch(item_ids, approved):
    request = main.BatchDecisionRequest(item_ids=item_ids, approved=approved)
    return asyncio.run(main.batch_queue_decision(request))


def test_batch_reject_reports_each_success(monkeypatch):
    items = {"a": {"id": "a"}, "b": {"id": "b"}}
    calls = []

    monkeypatch.setattr(main.queue_manager, "get_by_id", lambda item_id: items.get(item_id))

    async def fake_decide(item_id, request):
        calls.append((item_id, request.approved))
        return {"success": True, "action": "rejected", "file_written": None}

    monkeypatch.setattr(main, "_decide_queue_item", fake_decide)

    result = run_batch(["a", "b"], approved=False)

    assert result["success"] is True
    assert result["completed_count"] == 2
    assert result["failed_count"] == 0
    assert [row["item_id"] for row in result["results"]] == ["a", "b"]
    assert calls == [("a", False), ("b", False)]


def test_batch_approval_skips_not_ready_and_continues(monkeypatch):
    items = {
        "ready": {"id": "ready"},
        "pending": {"id": "pending", "pending_extraction": True},
        "failed": {"id": "failed", "extraction_error": "fetch failed"},
    }

    monkeypatch.setattr(main.queue_manager, "get_by_id", lambda item_id: items.get(item_id))

    async def fake_decide(item_id, request):
        return {"success": True, "action": "approved", "file_written": f"/{item_id}.md"}

    monkeypatch.setattr(main, "_decide_queue_item", fake_decide)

    result = run_batch(["ready", "pending", "failed"], approved=True)

    assert result["success"] is False
    assert result["completed_count"] == 1
    assert result["failed_count"] == 2
    assert [row.get("code") for row in result["results"]] == [None, "not_ready", "not_ready"]


def test_batch_returns_not_found_without_aborting(monkeypatch):
    monkeypatch.setattr(main.queue_manager, "get_by_id", lambda item_id: None)

    async def fake_decide(item_id, request):
        if item_id == "missing":
            raise HTTPException(404, f"Item {item_id} not found in queue")
        return {"success": True, "action": "rejected", "file_written": None}

    monkeypatch.setattr(main, "_decide_queue_item", fake_decide)

    result = run_batch(["missing", "other"], approved=False)

    assert result["completed_count"] == 1
    assert result["failed_count"] == 1
    assert result["results"][0]["code"] == "not_found"
    assert result["results"][1]["success"] is True


@pytest.mark.parametrize(
    "item_ids,message",
    [
        ([], "between 1 and 100"),
        (["same", "same"], "unique"),
        ([""], "non-empty"),
        ([f"id-{index}" for index in range(101)], "between 1 and 100"),
    ],
)
def test_batch_validates_request_scope(item_ids, message, monkeypatch):
    monkeypatch.setattr(main.queue_manager, "get_by_id", lambda item_id: None)

    with pytest.raises(HTTPException) as exc_info:
        run_batch(item_ids, approved=False)

    assert exc_info.value.status_code == 422
    assert message in str(exc_info.value.detail)
