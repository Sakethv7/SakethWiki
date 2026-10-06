import json
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import capture_compare as cc
import main
import wiki_writer

PAGE_BODY = (
    "# Parallel scan\n\n> **Current understanding** 🔵\n> Parallel scans split a table into segments.\n\n"
    "## Notes\n- A parallel scan with 8 segments finished in 204 seconds.\n"
    "- Each segment reads its own key range independently.\n"
)


def _page(vault: Path, slug: str, body: str = PAGE_BODY) -> Path:
    path = vault / "_wiki" / "cs" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\ntitle: "{slug}"\ntags: [aws]\nlast_updated: 2026-01-01\nentry_count: 1\n---\n{body}')
    return path


def _item(summary, page="parallel-scan"):
    return {"id": "i1", "title": "Parallel scan notes", "summary": summary, "suggested_page": page, "tags": ["aws"]}


def _llm(monkeypatch, verdicts):
    calls = []

    def fake(**kw):
        calls.append(kw)
        return json.dumps({"claims": [dict(i=i + 1, **v) for i, v in enumerate(verdicts)]})

    monkeypatch.setattr(cc.llm_client, "complete", fake)
    return calls


@pytest.mark.parametrize("verdicts,band", [
    (["same", "same"], "duplicate"),
    (["same", "new"], "overlap"),
    (["new"], "overlap"),
    (["same", "changed"], "conflict"),
    (["new", "conflicts"], "conflict"),
    ([], "unknown"),
])
def test_band_rules(verdicts, band):
    assert cc.band_from_verdicts(verdicts) == band


def test_identical_text_still_goes_through_claim_diff(isolated_vault, monkeypatch):
    # No token-overlap shortcut: only the claim diff can say "duplicate".
    _page(isolated_vault, "parallel-scan")
    calls = _llm(monkeypatch, [
        {"verdict": "same", "page_quote": "A parallel scan with 8 segments finished in 204 seconds."},
        {"verdict": "same", "page_quote": "Each segment reads its own key range independently."},
    ])
    report = cc.build_report(_item([
        "A parallel scan with 8 segments finished in 204 seconds.",
        "Each segment reads its own key range independently.",
    ]))
    assert report["band"] == "duplicate"
    assert len(calls) == 1


def test_changed_number_cannot_be_marked_same(isolated_vault, monkeypatch):
    _page(isolated_vault, "parallel-scan")
    _llm(monkeypatch, [{"verdict": "same", "page_quote": "A parallel scan with 8 segments finished in 204 seconds."}])
    report = cc.build_report(_item(["A parallel scan with 8 segments finished in 408 seconds."]))
    assert report["claims"][0]["verdict"] == "changed"
    assert report["band"] == "conflict"


def test_llm_failure_gives_unknown_and_never_raises(isolated_vault, monkeypatch):
    _page(isolated_vault, "parallel-scan")

    def boom(**kw):
        raise RuntimeError("provider down")

    monkeypatch.setattr(cc.llm_client, "complete", boom)
    report = cc.build_report(_item(["Segments should be sized to the table partition count."]))
    assert report["band"] == "unknown"
    assert report["target_page"] == "parallel-scan"
    assert report["recommended"] == "keep_both"


def test_no_close_page_is_distinct(isolated_vault, monkeypatch):
    _page(isolated_vault, "parallel-scan")
    _llm(monkeypatch, [])
    item = _item(["Opus 5 supports extended thinking budgets for long reasoning tasks."], page="opus-notes")
    assert cc.build_report(item)["band"] == "distinct"


def test_conflict_keeps_real_quote_and_drops_invented_one(isolated_vault, monkeypatch):
    _page(isolated_vault, "parallel-scan")
    _llm(monkeypatch, [
        {"verdict": "conflicts", "page_quote": "A parallel scan with 8 segments finished in 204 seconds."},
        {"verdict": "changed", "page_quote": "this sentence is not on the page"},
    ])
    report = cc.build_report(_item(["Scan took 90 seconds.", "Segments were read in order."]))
    assert report["band"] == "conflict"
    assert report["claims"][0]["page_quote"].startswith("A parallel scan")
    assert report["claims"][1]["page_quote"] is None
    assert report["recommended"] is None


def test_same_with_unrelated_quote_is_not_trusted(isolated_vault, monkeypatch):
    _page(isolated_vault, "parallel-scan")
    _llm(monkeypatch, [{"verdict": "same", "page_quote": "Each segment reads its own key range independently."}])
    report = cc.build_report(_item(["Hot partitions throttle writes when one key receives most traffic."]))
    assert report["claims"][0]["verdict"] == "new"
    assert report["band"] == "overlap"


def test_same_without_valid_quote_is_not_trusted(isolated_vault, monkeypatch):
    _page(isolated_vault, "parallel-scan")
    _llm(monkeypatch, [{"verdict": "same", "page_quote": "made up"}])
    report = cc.build_report(_item(["Segments should be sized to the table partition count."]))
    assert report["claims"][0]["verdict"] == "new"
    assert report["band"] == "overlap"


# ── resolutions ──────────────────────────────────────────────────────────────

def _approved(item, resolution):
    item = {**item, "resolution": resolution, "conflict_report": cc.build_report(item), "domain": "cs"}
    return item


def test_replace_archives_old_page_then_writes(isolated_vault, monkeypatch):
    path = _page(isolated_vault, "parallel-scan")
    old = path.read_bytes()
    _llm(monkeypatch, [{"verdict": "conflicts", "page_quote": "A parallel scan with 8 segments finished in 204 seconds."}])
    item = _approved(_item(["Scan took 90 seconds."]), "replace")
    wiki_writer.write_approved(item)
    archived = list((isolated_vault / "_wiki" / "meta" / "replaced").glob("parallel-scan-*.md"))
    assert len(archived) == 1 and archived[0].read_bytes() == old
    assert "90 seconds" in path.read_text()
    assert "204 seconds" not in path.read_text()


def test_replace_with_failing_archive_leaves_page_untouched(isolated_vault, monkeypatch):
    path = _page(isolated_vault, "parallel-scan")
    before = path.read_bytes()
    _llm(monkeypatch, [{"verdict": "conflicts", "page_quote": "A parallel scan with 8 segments finished in 204 seconds."}])
    item = _approved(_item(["Scan took 90 seconds."]), "replace")

    def fail(*a, **k):
        raise wiki_writer.ArchiveError("disk full")

    monkeypatch.setattr(wiki_writer, "_archive_page", fail)
    with pytest.raises(wiki_writer.ArchiveError):
        wiki_writer.write_approved(item)
    assert path.read_bytes() == before


def test_keep_both_writes_new_slug_and_links_pages(isolated_vault, monkeypatch):
    path = _page(isolated_vault, "parallel-scan")
    _llm(monkeypatch, [{"verdict": "conflicts", "page_quote": "A parallel scan with 8 segments finished in 204 seconds."}])
    item = _approved(_item(["Scan took 90 seconds."]), "keep_both")
    rel = wiki_writer.write_approved(item)
    new = isolated_vault / rel
    assert new.stem == "parallel-scan-2"
    assert "[[parallel-scan]]" in new.read_text()
    assert "[[parallel-scan-2]]" in path.read_text()
    assert "204 seconds" in path.read_text()  # original claims untouched


def test_append_never_drops_clip_even_if_classifier_says_duplicates(isolated_vault, monkeypatch):
    path = _page(isolated_vault, "parallel-scan")
    _llm(monkeypatch, [{"verdict": "new", "page_quote": None}])
    monkeypatch.setattr(wiki_writer, "_analyze_evolution", lambda existing, item: {
        "evolution_type": "duplicates", "evolution_reason": "x", "updated_understanding": "Parallel scans split tables."})
    item = _approved(_item(["Segments should match the partition count."]), "append")
    rel = wiki_writer.write_approved(item)
    assert "[duplicate" not in rel
    assert "partition count" in path.read_text()


# ── approve endpoint ─────────────────────────────────────────────────────────

def test_approve_with_stale_report_returns_409_and_writes_nothing(isolated_vault, monkeypatch):
    path = _page(isolated_vault, "parallel-scan")
    _llm(monkeypatch, [{"verdict": "new", "page_quote": None}])
    item = {**_item(["Segments should match the partition count."]), "id": "item-1"}
    item["conflict_report"] = cc.build_report(item)
    path.write_text(path.read_text() + "\n- Edited after the report was built.\n")
    written = []
    monkeypatch.setattr(main.queue_manager, "get_by_id", lambda i: item)
    monkeypatch.setattr(main.wiki_writer, "write_approved", lambda it: written.append(it) or "x")
    with pytest.raises(HTTPException) as err:
        import asyncio
        asyncio.run(main._decide_queue_item("item-1", main.ApproveRequest(approved=True, resolution="append")))
    assert err.value.status_code == 409
    assert err.value.detail["error"] == "report_stale"
    assert written == []


def test_batch_approve_blocks_items_that_need_review(monkeypatch):
    import asyncio
    item = {"id": "a", "title": "T", "conflict_report": {"band": "conflict", "target_page": "p"}}
    monkeypatch.setattr(main.queue_manager, "get_by_id", lambda i: item)
    decided = []

    async def decide(*a, **k):
        decided.append(a)
        return {"success": True}

    monkeypatch.setattr(main, "_decide_queue_item", decide)
    out = asyncio.run(main.batch_queue_decision(main.BatchDecisionRequest(item_ids=["a"], approved=True)))
    assert out["results"][0]["code"] == "needs_review"
    assert decided == []


def test_conflict_flag_with_unrelated_quote_is_demoted(isolated_vault, monkeypatch):
    _page(isolated_vault, "parallel-scan")
    _llm(monkeypatch, [{"verdict": "conflicts", "page_quote": "Each segment reads its own key range independently."}])
    report = cc.build_report(_item(["Hot partitions throttle writes when one key receives most traffic."]))
    assert report["claims"][0]["verdict"] == "new"
    assert report["claims"][0]["page_quote"] is None


def test_exact_repaste_raises_friendly_duplicate_with_page(isolated_vault, monkeypatch):
    monkeypatch.setattr(main.queue_manager, "get_all", lambda: [])
    page = _page(isolated_vault, "parallel-scan")
    sig = main._clip_signature("# Parallel scan\n\n- one claim here\n- another claim here", "", "")
    idx = isolated_vault / "_wiki" / "meta" / "processed_clips.jsonl"
    idx.write_text(json.dumps({"clip_signature": sig, "file_written": "_wiki/cs/parallel-scan.md"}) + "\n")
    monkeypatch.setattr(main, "_extract_with_sonnet", lambda *a, **k: pytest.fail("must not extract"))
    with pytest.raises(main.DuplicateClip) as dup:
        main._stage_markdown_clip("# Parallel scan\n\n- one claim here\n- another claim here")
    assert dup.value.page == "parallel-scan"
    assert "already in 'parallel-scan'" in dup.value.report["reason"]
    page.unlink()
    monkeypatch.setattr(main, "_extract_with_sonnet", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("reached extraction")))
    with pytest.raises(RuntimeError):   # page deleted: the ledger no longer blocks, so extraction runs
        main._stage_markdown_clip("# Parallel scan\n\n- one claim here\n- another claim here")


# ── ADR 7: no blind approve ──────────────────────────────────────────────────

def _decide(monkeypatch, item, **req):
    import asyncio
    written = []
    monkeypatch.setattr(main.queue_manager, "get_by_id", lambda i: item)
    monkeypatch.setattr(main.queue_manager, "update", lambda i, it: True)
    monkeypatch.setattr(main.queue_manager, "remove", lambda i: True)
    monkeypatch.setattr(main.wiki_writer, "write_approved", lambda it: written.append(it) or "_wiki/cs/x.md")
    monkeypatch.setattr(main.wiki_writer, "fix_page_wikilinks", lambda p: None)
    monkeypatch.setattr(main.memory_store, "index_page", lambda p: None)
    monkeypatch.setattr(main, "_record_processed_clip", lambda it, p: None)
    try:
        return asyncio.run(main._decide_queue_item("i1", main.ApproveRequest(approved=True, **req))), written
    except HTTPException as e:
        return e, written


def _queued(band, target="p"):
    return {"id": "i1", "title": "T", "summary": ["a claim"], "suggested_page": "p", "tags": [],
            "conflict_report": {"band": band, "target_page": target, "claims": []}}


def test_approve_without_resolution_is_refused_for_conflict(monkeypatch):
    err, written = _decide(monkeypatch, _queued("conflict"))
    assert isinstance(err, HTTPException) and err.status_code == 409
    assert err.detail["error"] == "needs_review" and err.detail["band"] == "conflict"
    assert written == []


def test_approve_without_resolution_still_writes_distinct(monkeypatch):
    out, written = _decide(monkeypatch, _queued("distinct", target=None))
    assert out["success"] and len(written) == 1


def test_old_items_without_a_report_are_not_blocked(monkeypatch):
    item = _queued("conflict")
    del item["conflict_report"]
    out, written = _decide(monkeypatch, item)
    assert out["success"] and len(written) == 1


def test_late_extraction_compares_and_blocks(monkeypatch):
    item = {"id": "i1", "url": "https://x.test/a", "title": "u", "summary": ["…"], "pending_extraction": True,
            "suggested_page": "unprocessed", "tags": []}
    monkeypatch.setattr(main, "_fetch_url", lambda u: "text")
    monkeypatch.setattr(main.vault_reader, "list_concept_pages", lambda: [])
    monkeypatch.setattr(main, "_extract_with_sonnet", lambda *a, **k: {
        "title": "Late", "key_concepts": [], "summary": ["new claim"], "suggested_page": "p",
        "suggested_wikilinks": [], "tags": [], "references": [], "diagram": ""})
    monkeypatch.setattr(main, "_curation_fields", lambda d: {})
    monkeypatch.setattr(main.capture_compare, "build_report",
                        lambda it: {"band": "conflict", "target_page": "p", "claims": []})
    err, written = _decide(monkeypatch, item)
    assert isinstance(err, HTTPException) and err.detail["error"] == "needs_review"
    assert written == [] and item["conflict_report"]["band"] == "conflict"


def _direct(monkeypatch, band, target="p"):
    import asyncio
    queued, written = [], []
    monkeypatch.setattr(main, "_extract_with_sonnet", lambda *a, **k: {
        "title": "T", "key_concepts": [], "summary": ["claim"], "suggested_page": "p",
        "suggested_wikilinks": [], "tags": [], "references": [], "diagram": ""})
    monkeypatch.setattr(main.vault_reader, "list_concept_pages", lambda: [])
    monkeypatch.setattr(main, "_curation_fields", lambda d: {})
    monkeypatch.setattr(main.capture_compare, "build_report",
                        lambda it: {"band": band, "target_page": target, "claims": [], "reason": "r"})
    monkeypatch.setattr(main.queue_manager, "enqueue", queued.append)
    monkeypatch.setattr(main.wiki_writer, "write_approved", lambda it: written.append(it) or "_wiki/cs/p.md")
    monkeypatch.setattr(main.wiki_writer, "fix_page_wikilinks", lambda p: None)
    monkeypatch.setattr(main.memory_store, "index_page", lambda p: None)
    monkeypatch.setattr(main.tag_classifier, "classify_new_tags", lambda *a, **k: None)
    out = asyncio.run(main.ingest_direct(main.IngestRequest(text="some text", force=True)))
    return out, queued, written


def test_ingest_direct_writes_distinct(monkeypatch):
    out, queued, written = _direct(monkeypatch, "distinct", target=None)
    assert out["success"] and len(written) == 1 and queued == []


def test_ingest_direct_queues_conflict_instead_of_writing(monkeypatch):
    out, queued, written = _direct(monkeypatch, "conflict")
    assert out["needs_review"] and len(queued) == 1 and written == []
    assert queued[0]["status"] == "pending" and queued[0]["conflict_report"]["band"] == "conflict"


def test_ingest_direct_does_not_write_duplicate(monkeypatch):
    out, queued, written = _direct(monkeypatch, "duplicate")
    assert out["queued"] is False and out["reason"] == "duplicate" and queued == [] and written == []
