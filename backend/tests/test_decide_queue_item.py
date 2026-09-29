import asyncio
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


ITEM = {
    "id": "item-1",
    "url": "https://example.com/post",
    "title": "Backpressure in queues",
    "summary": ["Queues need limits", "Producers slow down when consumers lag"],
    "suggested_page": "backpressure",
    "suggested_wikilinks": [],
    "tags": ["systems"],
}


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Stub every side effect of _decide_queue_item and record what happened."""
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    state = {"item": dict(ITEM), "removed": [], "written": [], "traces": [], "deep_dive": []}

    monkeypatch.setattr(main.queue_manager, "get_by_id", lambda item_id: state["item"] if item_id == "item-1" else None)
    monkeypatch.setattr(main.queue_manager, "remove", state["removed"].append)

    def write_approved(item):
        state["written"].append(dict(item))
        return str(tmp_path / f"{item['suggested_page']}.md")

    monkeypatch.setattr(main.wiki_writer, "write_approved", write_approved)
    monkeypatch.setattr(main.wiki_writer, "fix_page_wikilinks", lambda page: None)
    monkeypatch.setattr(main.memory_store, "index_page", lambda page: None)
    monkeypatch.setattr(main, "_record_processed_clip", lambda item, path: None)
    monkeypatch.setattr(main.tag_classifier, "classify_new_tags", lambda tags, key: None)
    monkeypatch.setattr(main, "_append_trace", state["traces"].append)
    monkeypatch.setattr(main.preference_memory, "record_approval_trace", lambda trace: None)
    monkeypatch.setattr(main, "_add_deep_dive_tag", state["deep_dive"].append)
    return state


def decide(approved, edits=None, open_thread=False, item_id="item-1"):
    req = main.ApproveRequest(approved=approved, edits=edits, open_thread=open_thread)
    return asyncio.run(main._decide_queue_item(item_id, req))


def test_missing_item_is_404(env):
    with pytest.raises(HTTPException) as exc:
        decide(approved=True, item_id="nope")
    assert exc.value.status_code == 404
    assert env["removed"] == [] and env["written"] == []


def test_reject_removes_item_without_writing(env):
    result = decide(approved=False)

    assert result == {"success": True, "action": "rejected", "file_written": None}
    assert env["removed"] == ["item-1"]
    assert env["written"] == []
    assert env["traces"][0]["approved"] is False


def test_reject_survives_trace_failure(env, monkeypatch):
    def broken(trace):
        raise OSError("disk full")

    monkeypatch.setattr(main, "_append_trace", broken)
    assert decide(approved=False)["action"] == "rejected"
    assert env["removed"] == ["item-1"]


def test_approve_writes_then_removes(env):
    result = decide(approved=True)

    assert result["action"] == "approved"
    assert result["file_written"].endswith("backpressure.md")
    assert len(env["written"]) == 1
    assert env["removed"] == ["item-1"]
    trace = env["traces"][0]
    assert trace["approved"] is True
    assert trace["page_corrected"] is False
    assert trace["tags_corrected"] is False
    assert trace["deep_dive"] is False


def test_failed_vault_write_keeps_item_in_queue(env, monkeypatch):
    def broken(item):
        raise OSError("vault locked")

    monkeypatch.setattr(main.wiki_writer, "write_approved", broken)

    with pytest.raises(HTTPException) as exc:
        decide(approved=True)
    assert exc.value.status_code == 500
    assert env["removed"] == []
    assert env["traces"] == []


def test_human_edits_are_applied_and_traced_as_corrections(env):
    decide(approved=True, edits={"suggested_page": "Flow Control", "tags": ["networking"], "url": "https://evil.example"})

    written = env["written"][0]
    assert written["suggested_page"] == "flow-control"
    assert written["tags"] == ["networking"]
    # url is not an editable field
    assert written["url"] == ITEM["url"]

    trace = env["traces"][0]
    assert trace["suggested_page"] == "backpressure"
    assert trace["final_page"] == "flow-control"
    assert trace["page_corrected"] is True
    assert trace["tags_suggested"] == ["systems"]
    assert trace["tags_final"] == ["networking"]
    assert trace["tags_corrected"] is True


def test_wikilinks_resolve_through_aliases(env):
    env["item"]["suggested_wikilinks"] = ["Retrieval Augmented Generation", "Agent"]
    decide(approved=True)
    assert env["written"][0]["suggested_wikilinks"] == ["rag", "agents"]


def test_post_write_failures_do_not_undo_approval(env, monkeypatch):
    def broken(*args):
        raise RuntimeError("side effect failed")

    monkeypatch.setattr(main.memory_store, "index_page", broken)
    monkeypatch.setattr(main.tag_classifier, "classify_new_tags", broken)
    monkeypatch.setattr(main.wiki_writer, "fix_page_wikilinks", broken)

    result = decide(approved=True)
    assert result["action"] == "approved"
    assert env["removed"] == ["item-1"]


def test_pending_extraction_runs_before_write(env, monkeypatch):
    env["item"]["pending_extraction"] = True
    monkeypatch.setattr(main, "_fetch_url", lambda url: "raw page text")
    monkeypatch.setattr(main.vault_reader, "list_concept_pages", lambda: [])
    monkeypatch.setattr(main, "_extract_with_sonnet", lambda text, images, url, pages: {
        "title": "Extracted title",
        "summary": ["Extracted point"],
        "suggested_page": "extracted-page",
        "tags": ["extracted"],
    })

    decide(approved=True)

    written = env["written"][0]
    assert written["title"] == "Extracted title"
    assert written["suggested_page"] == "extracted-page"
    assert written["pending_extraction"] is False


def test_failed_pending_extraction_still_writes_original(env, monkeypatch):
    env["item"]["pending_extraction"] = True

    def broken(url):
        raise OSError("fetch failed")

    monkeypatch.setattr(main, "_fetch_url", broken)

    result = decide(approved=True)

    assert result["action"] == "approved"
    assert env["written"][0]["title"] == ITEM["title"]


def test_open_thread_tags_page_for_deep_dive(env):
    decide(approved=True, open_thread=True)
    assert len(env["deep_dive"]) == 1
    assert env["traces"][0]["deep_dive"] is True
