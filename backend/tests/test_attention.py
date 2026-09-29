import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


def test_attention_groups_and_excludes_paired_orphans(monkeypatch):
    review = [
        {"name": "conflicted", "folder": "cs", "priority": "high", "reasons": ["2 unresolved conflict marker(s)"]},
        {"name": "lonely", "folder": "cs", "priority": "high", "reasons": ["no backlinks", "thin page"]},
        {"name": "twin-a", "folder": "cs", "priority": "high", "reasons": ["no backlinks"]},
        {"name": "fine", "folder": "cs", "priority": "low", "reasons": ["no backlinks"]},
    ]
    pairs = [{"source": "twin-a", "target": "twin-b", "score": 0.55, "reasons": ["similar slugs (0.9)"]}]
    seen = {}

    monkeypatch.setattr(main.active_review, "build_queue", lambda limit, min_priority: review)
    monkeypatch.setattr(main.consolidation, "find_candidates", lambda limit, include_weak: pairs)

    def fake_partners(slugs, exclude_pairs):
        seen["slugs"], seen["exclude"] = slugs, exclude_pairs
        return {s: {"source": s, "target": "partner", "score": 0.4, "reasons": []} for s in slugs}

    monkeypatch.setattr(main.consolidation, "best_partners", fake_partners)

    result = main.attention()
    assert [c["name"] for c in result["contradictions"]] == ["conflicted"]
    assert result["pairs"] == pairs
    assert seen["slugs"] == ["lonely"]                  # twin-a is already in a pair; "fine" isn't high priority
    assert ("twin-a", "twin-b") in seen["exclude"]
    assert result["orphans"][0]["source"] == "lonely"
    assert result["counts"]["unlinked_total"] == 1
