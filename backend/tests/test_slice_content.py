import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main


def _paragraphs(count=6, size=900):
    return "\n\n".join(f"Paragraph {i} " + ("concept detail " * (size // 15)) for i in range(count))


def test_heading_structure_bypasses_llm(monkeypatch):
    text = "# Authorization\n\n" + ("A " * 700) + "\n\n# Sandboxing\n\n" + ("B " * 700)
    monkeypatch.setattr(main.llm_client, "complete", lambda **_: (_ for _ in ()).throw(AssertionError("planner should not run")))

    result = main._slice_content(text, "", [])

    assert [item["title"] for item in result] == ["Authorization", "Sandboxing"]
    assert all(item["split_mode"] == "headings" for item in result)


def test_planner_failure_uses_conservative_chunks(monkeypatch):
    text = _paragraphs()
    monkeypatch.setattr(main.llm_client, "complete", lambda **_: (_ for _ in ()).throw(RuntimeError("provider down")))

    result = main._slice_content(text, "", [])

    assert len(result) >= 2
    assert all(item["split_mode"] == "paragraph_chunks" for item in result)
    assert "Paragraph 0" in result[0]["text"]


def test_planner_must_cover_each_paragraph_exactly_once(monkeypatch):
    text = _paragraphs(4, 1500)
    monkeypatch.setattr(
        main.llm_client,
        "complete",
        lambda **_: json.dumps([
            {"title": "first", "paragraphs": [0, 1], "concept_hint": ""},
            {"title": "bad", "paragraphs": [1, 2, 3], "concept_hint": ""},
        ]),
    )

    result = main._slice_content(text, "", [])

    assert len(result) >= 2
    assert all(item["split_mode"] == "paragraph_chunks" for item in result)


def test_fallback_merges_overflow_without_dropping_content(monkeypatch):
    text = "\n\n".join(f"# Section {i}\n\n" + (f"detail-{i} " * 600) for i in range(8))
    monkeypatch.setattr(main.llm_client, "complete", lambda **_: (_ for _ in ()).throw(RuntimeError("provider down")))

    result = main._slice_content(text, "", [])

    assert len(result) == 5
    combined = "\n\n".join(item["text"] for item in result)
    for i in range(8):
        assert f"detail-{i}" in combined
