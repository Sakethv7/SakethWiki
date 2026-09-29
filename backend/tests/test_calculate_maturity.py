import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


def page(version: int, sections: int, last_updated: str = "2000-01-01", body: str = "") -> str:
    fm = f"---\ntitle: Test\nlast_updated: {last_updated}\nunderstanding_version: {version}\n---\n\n# Test\n\n"
    return fm + body + "".join(f"## Source {i}\n\nText.\n\n" for i in range(sections))


@pytest.fixture
def wiki(monkeypatch, tmp_path):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    root = tmp_path / "_wiki"
    (root / "cs").mkdir(parents=True)
    (root / "meta").mkdir()
    return root


def maturity(name: str) -> dict:
    return asyncio.run(main.calculate_maturity(name))


def test_missing_page_is_404(wiki):
    with pytest.raises(HTTPException) as exc:
        maturity("nope")
    assert exc.value.status_code == 404


def test_page_without_frontmatter_is_400(wiki):
    (wiki / "cs" / "bare.md").write_text("# No frontmatter\n")
    with pytest.raises(HTTPException) as exc:
        maturity("bare")
    assert exc.value.status_code == 400


def test_low_signal_page(wiki):
    # version 1 -> 5, 1 source -> 4, 0 backlinks -> 0,
    # never read and stale -> activity floor 20 -> 2, one warning -> 2.5. Total 13.5.
    (wiki / "cs" / "thin.md").write_text(page(1, 1, body="> [!warning] conflicting claims\n\n"))

    result = maturity("thin")

    assert result["understanding_maturity"] == 13
    c = result["components"]
    assert c["backlink_count"] == 0
    assert c["source_count"] == 1
    assert c["activity_score"] == 20
    assert c["contradiction_count"] == 1


def test_saturated_page_scores_100(wiki):
    # version 5 -> 25, 5 sources -> 20, 8 backlinks -> 40, read today -> 10, no warnings -> 5.
    (wiki / "cs" / "pillar.md").write_text(page(5, 5))
    for i in range(8):
        (wiki / "cs" / f"ref-{i}.md").write_text(f"See [[pillar]] ({i}).\n")
    read = {"concept": "pillar", "ts": datetime.now().isoformat()}
    (wiki / "meta" / "reads.jsonl").write_text(json.dumps(read) + "\n")

    result = maturity("pillar")

    assert result["understanding_maturity"] == 100
    assert result["components"]["backlink_count"] == 8
    assert result["components"]["days_since_read"] == 0


def test_caps_sources_and_ignores_self_links(wiki):
    (wiki / "cs" / "busy.md").write_text(page(1, 9, body="Self link [[busy]].\n\n"))

    c = maturity("busy")["components"]

    assert c["source_count"] == 9
    assert c["source_count_capped"] == 5
    assert c["source_score"] == 20
    assert c["backlink_count"] == 0


def test_writes_score_into_frontmatter(wiki):
    path = wiki / "cs" / "thin.md"
    path.write_text(page(1, 1))

    first = maturity("thin")["understanding_maturity"]
    assert f"understanding_maturity: {first}\nunderstanding_version: 1" in path.read_text()

    # A second run updates the existing field instead of adding another.
    (wiki / "cs" / "ref.md").write_text("[[thin]]\n")
    second = maturity("thin")["understanding_maturity"]
    text = path.read_text()
    assert second > first
    assert text.count("understanding_maturity:") == 1
    assert f"understanding_maturity: {second}" in text
