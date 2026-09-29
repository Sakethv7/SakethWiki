import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import consolidation


def test_each_slug_resolved_and_parsed_once_per_run(monkeypatch):
    slugs = [f"page-{i}" for i in range(6)]
    resolved, parsed = Counter(), Counter()

    monkeypatch.setattr(consolidation.vault_reader, "list_concept_pages", lambda: [{"name": s} for s in slugs])
    monkeypatch.setattr(consolidation.identity, "duplicate_candidates", lambda _slugs: [])

    def fake_resolve(slug):
        resolved[slug] += 1
        return slug

    def fake_parse(slug):
        parsed[slug] += 1
        return {"title": slug, "current_understanding": "", "tags": []}

    monkeypatch.setattr(consolidation.identity, "resolve_slug", fake_resolve)
    monkeypatch.setattr(consolidation.vault_reader, "parse_concept_page", fake_parse)

    consolidation.find_candidates(include_weak=True)
    assert set(resolved.values()) == {1}
    assert set(parsed.values()) == {1}

    # A second run clears the caches, so edits between runs are picked up.
    consolidation.find_candidates(include_weak=True)
    assert set(resolved.values()) == {2}


def _fake_vault(monkeypatch, tmp_path, texts):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    monkeypatch.setattr(consolidation.vault_reader, "list_concept_pages", lambda: [{"name": s} for s in texts])
    monkeypatch.setattr(consolidation.identity, "duplicate_candidates", lambda _s: [])
    monkeypatch.setattr(consolidation.identity, "resolve_slug", lambda s: s)
    monkeypatch.setattr(consolidation.vault_reader, "parse_concept_page",
                        lambda s: {"title": s, "current_understanding": texts[s], "tags": []})


def test_best_partner_picks_closest_and_skips_dismissed(monkeypatch, tmp_path):
    _fake_vault(monkeypatch, tmp_path, {
        "memory-agents": "agents with long term memory stores",
        "memory-augmented-agents": "agents augmented with long term memory",
        "gpu-kernels": "cuda kernels and warps",
    })
    best = consolidation.best_partners(["memory-agents"])
    assert best["memory-agents"]["target"] == "memory-augmented-agents"

    consolidation.dismiss_pair("memory-agents", "memory-augmented-agents")
    best = consolidation.best_partners(["memory-agents"])
    assert best["memory-agents"]["target"] == "gpu-kernels"   # next-best once dismissed
