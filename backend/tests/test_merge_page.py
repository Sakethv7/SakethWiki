import asyncio
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import capture_compare as cc
import main
import merge_page
import wiki_writer
from tests.test_capture_compare import PAGE_BODY, _item, _page  # noqa: F401  (isolated_vault is a conftest fixture)

MERGED = (
    "# Parallel scan\n\n> **Current understanding** 🔵\n> Parallel scans split a table into segments. Segments should match partitions.\n\n"
    "## Notes\n- A parallel scan with 8 segments finished in 204 seconds.\n"
    "- Each segment reads its own key range independently.\n- Segments should match the partition count.\n"
)


def _model(monkeypatch, text):
    monkeypatch.setattr(merge_page.llm_client, "complete", lambda **kw: text)


def _queued(vault):
    _page(vault, "parallel-scan")
    item = {**_item(["Segments should match the partition count."]), "id": "i1", "domain": "cs"}
    item["conflict_report"] = {"band": "overlap", "target_page": "parallel-scan", "target_hash": cc.page_hash("parallel-scan"), "claims": []}
    return item


def test_build_merge_returns_preview_and_hash_and_writes_nothing(isolated_vault, monkeypatch):
    item = _queued(isolated_vault)
    before = (isolated_vault / "_wiki" / "cs" / "parallel-scan.md").read_bytes()
    _model(monkeypatch, "```markdown\n" + MERGED + "```")
    out = merge_page.build_merge(item)
    assert out["merged_body"].startswith("# Parallel scan") and "```" not in out["merged_body"]
    assert out["target_hash"] == cc.page_hash("parallel-scan")
    assert (isolated_vault / "_wiki" / "cs" / "parallel-scan.md").read_bytes() == before


@pytest.mark.parametrize("bad,why", [
    ("", "no text"),
    ("# Parallel scan\n\nShort.", "shorter"),
    (MERGED.replace("# Parallel scan", "# Other title", 1), "heading"),
    (MERGED.replace("204", "x").replace("8 segments", "some segments"), "numbers"),
])
def test_bad_model_output_is_rejected(isolated_vault, monkeypatch, bad, why):
    item = _queued(isolated_vault)
    _model(monkeypatch, bad)
    with pytest.raises(merge_page.MergeError) as err:
        merge_page.build_merge(item)
    assert err.value.kind == "merge_rejected"


def test_model_failure_and_missing_target(isolated_vault, monkeypatch):
    item = _queued(isolated_vault)
    def boom(**kw):
        raise RuntimeError("down")
    monkeypatch.setattr(merge_page.llm_client, "complete", boom)
    with pytest.raises(merge_page.MergeError) as err:
        merge_page.build_merge(item)
    assert err.value.kind == "model_failed"
    with pytest.raises(merge_page.MergeError) as err:
        merge_page.build_merge({**item, "conflict_report": {"target_page": None}})
    assert err.value.kind == "no_target"


def test_writer_archives_then_writes_merged_body_with_old_frontmatter(isolated_vault):
    item = _queued(isolated_vault)
    path = isolated_vault / "_wiki" / "cs" / "parallel-scan.md"
    old = path.read_bytes()
    wiki_writer.write_approved({**item, "resolution": "merge", "merged_body": MERGED})
    archived = list((isolated_vault / "_wiki" / "meta" / "replaced").glob("parallel-scan-*.md"))
    assert len(archived) == 1 and archived[0].read_bytes() == old
    text = path.read_text()
    assert text.startswith("---\ntitle: \"parallel-scan\"") and "entry_count: 2" in text
    assert "Segments should match the partition count." in text and "204 seconds" in text


def _decide(item, **req):
    return asyncio.run(main._decide_queue_item("i1", main.ApproveRequest(approved=True, **req)))


def _wire(monkeypatch, item):
    written = []
    monkeypatch.setattr(main.queue_manager, "get_by_id", lambda i: item)
    monkeypatch.setattr(main.queue_manager, "update", lambda i, it: True)
    monkeypatch.setattr(main.queue_manager, "remove", lambda i: True)
    monkeypatch.setattr(main.wiki_writer, "write_approved", lambda it: written.append(it) or "_wiki/cs/parallel-scan.md")
    monkeypatch.setattr(main.wiki_writer, "fix_page_wikilinks", lambda p: None)
    monkeypatch.setattr(main.memory_store, "index_page", lambda p: None)
    monkeypatch.setattr(main, "_record_processed_clip", lambda it, p: None)
    return written


def test_approve_merge_needs_body_and_current_hash(isolated_vault, monkeypatch):
    item = _queued(isolated_vault)
    written = _wire(monkeypatch, item)
    with pytest.raises(HTTPException) as err:
        _decide(item, resolution="merge")
    assert err.value.status_code == 422
    with pytest.raises(HTTPException) as err:
        _decide(item, resolution="merge", merged_body=MERGED, merge_target_hash="old-hash")
    assert err.value.status_code == 409 and err.value.detail["error"] == "report_stale"
    assert written == []
    out = _decide(item, resolution="merge", merged_body=MERGED, merge_target_hash=cc.page_hash("parallel-scan"))
    assert out["success"] and written[0]["merged_body"] == MERGED


def test_merge_preview_route_maps_errors(isolated_vault, monkeypatch):
    item = _queued(isolated_vault)
    monkeypatch.setattr(main.queue_manager, "get_by_id", lambda i: item)
    _model(monkeypatch, "")
    with pytest.raises(HTTPException) as err:
        asyncio.run(main.merge_preview("i1"))
    assert err.value.status_code == 422 and err.value.detail["error"] == "merge_rejected"
    _model(monkeypatch, MERGED)
    assert asyncio.run(main.merge_preview("i1"))["target_page"] == "parallel-scan"
