import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import resolve_reviews


def test_select_system_candidates_splits_by_risk_and_status(monkeypatch):
    candidates = [
        {"id": "low1", "risk": "low", "status": "candidate", "requires_approval": False},
        {"id": "med-ready", "risk": "medium", "status": "eval_ready", "requires_approval": False},
        {"id": "med-pending", "risk": "medium", "status": "candidate", "requires_approval": False},
        {"id": "high1", "risk": "high", "status": "needs_approval", "requires_approval": True},
        {"id": "done", "risk": "low", "status": "applied", "requires_approval": False},
    ]
    monkeypatch.setattr(resolve_reviews.system_loop, "list_action_candidates", lambda: candidates)

    eligible, left = resolve_reviews.select_system_candidates()

    assert [c["id"] for c in eligible] == ["low1", "med-ready"]
    assert [c["id"] for c in left] == ["med-pending", "high1"]


def test_select_hitl_items_only_ingest_and_ready_are_eligible():
    items = [
        {"id": "a", "title": "A", "source_verdict": "ingest"},
        {"id": "b", "title": "B", "source_verdict": "reject"},
        {"id": "c", "title": "C", "source_verdict": "source_only"},
        {"id": "d", "title": "D", "source_verdict": "ingest", "pending_extraction": True},
        {"id": "e", "title": "E", "source_verdict": "ingest", "extraction_error": "fetch failed"},
    ]

    def handler(request):
        assert request.url.path == "/queue"
        return httpx.Response(200, json={"items": items})

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as client:
        eligible, left = resolve_reviews.select_hitl_items(client)

    assert [i["id"] for i in eligible] == ["a"]
    # "d" and "e" are not extraction-ready, so they're skipped entirely —
    # not eligible, but also not surfaced as "left for human" noise.
    assert [i["id"] for i in left] == ["b", "c"]


def test_apply_hitl_items_reports_per_item_results():
    eligible = [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}]

    def handler(request):
        assert request.url.path == "/queue/batch-decision"
        return httpx.Response(200, json={
            "results": [
                {"item_id": "a", "success": True, "action": "approved"},
                {"item_id": "b", "success": False, "code": "not_found"},
            ]
        })

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test") as client:
        results = resolve_reviews.apply_hitl_items(client, eligible, limit=None)

    assert results == [
        {"id": "a", "title": "A", "success": True, "status": "approved"},
        {"id": "b", "title": "B", "success": False, "status": "not_found"},
    ]


def test_apply_system_candidates_reports_failures(monkeypatch):
    eligible = [{"id": "x", "risk": "low", "reason": "test"}]

    def raise_error(candidate_id):
        raise ValueError("boom")

    monkeypatch.setattr(resolve_reviews.system_loop, "approve_action_candidate", raise_error)

    results = resolve_reviews.apply_system_candidates(eligible, limit=None)

    assert results == [{"id": "x", "title": "test", "success": False, "status": "error: boom"}]
