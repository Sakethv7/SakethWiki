import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import consolidation
import main


@pytest.fixture
def vault(monkeypatch, tmp_path):
    cs = tmp_path / "_wiki" / "cs"
    cs.mkdir(parents=True)
    (cs / "old-page.md").write_text("---\ntitle: Old\n---\nold body\n", encoding="utf-8")
    (cs / "kept-page.md").write_text("---\ntitle: Kept\n---\nkept body\n", encoding="utf-8")
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    calls = []
    monkeypatch.setattr(main, "_draft_merge", lambda *a: calls.append(a) or "---\ntitle: Kept\n---\nmerged body\n")
    monkeypatch.setattr(main.memory_store, "remove_page", lambda *_: None)
    monkeypatch.setattr(main.memory_store, "index_page", lambda *_: None)
    monkeypatch.setattr(main.wiki_writer, "_update_index", lambda: None)
    return tmp_path, calls


def request(**kw):
    return main.ConsolidateRequest(source="old-page", target="kept-page", force=True, **kw)


def test_dry_run_returns_draft_and_writes_nothing(vault):
    root, calls = vault
    result = main.consolidate(request(dry_run=True))
    assert "merged body" in result["preview"]
    assert result["source_sha"] and result["target_sha"]
    assert (root / "_wiki" / "cs" / "old-page.md").exists()
    assert "kept body" in (root / "_wiki" / "cs" / "kept-page.md").read_text()
    assert len(calls) == 1


def test_apply_writes_draft_backs_up_and_deletes_source_without_llm(vault):
    root, calls = vault
    draft = main.consolidate(request(dry_run=True))
    calls.clear()
    result = main.consolidate(request(merged=draft["preview"], source_sha=draft["source_sha"], target_sha=draft["target_sha"]))
    assert calls == []                                        # no second LLM call
    assert not (root / "_wiki" / "cs" / "old-page.md").exists()
    assert "merged body" in (root / "_wiki" / "cs" / "kept-page.md").read_text()
    backup = root / result["backup"]
    assert (backup / "old-page.md").read_text().endswith("old body\n")
    assert (backup / "kept-page.md").read_text().endswith("kept body\n")


def test_apply_refuses_if_a_page_changed_after_preview(vault):
    root, _ = vault
    draft = main.consolidate(request(dry_run=True))
    (root / "_wiki" / "cs" / "kept-page.md").write_text("---\ntitle: Kept\n---\nedited\n", encoding="utf-8")
    with pytest.raises(HTTPException) as exc:
        main.consolidate(request(merged=draft["preview"], source_sha=draft["source_sha"], target_sha=draft["target_sha"]))
    assert exc.value.status_code == 409
    assert (root / "_wiki" / "cs" / "old-page.md").exists()


def test_dismissed_pair_is_hidden_in_either_order(monkeypatch, tmp_path):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    monkeypatch.setattr(consolidation.vault_reader, "list_concept_pages",
                        lambda: [{"name": "data-prep-genai"}, {"name": "data-prep-ml"}])
    monkeypatch.setattr(consolidation.identity, "duplicate_candidates", lambda _s: [])
    monkeypatch.setattr(consolidation.identity, "resolve_slug", lambda s: s)
    monkeypatch.setattr(consolidation.vault_reader, "parse_concept_page",
                        lambda s: {"title": "data prep", "current_understanding": "data preparation", "tags": []})

    assert consolidation.find_candidates(include_weak=True)
    consolidation.dismiss_pair("data-prep-ml", "data-prep-genai")
    assert consolidation.find_candidates(include_weak=True) == []
