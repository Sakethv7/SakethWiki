import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import diagram_fit as df

LR5 = """flowchart LR
    A["Start"] --> B["Second step"]
    B --> C["Third step"]
    C --> D["Fourth step"]
    D --> E["Done"]"""


def _ids(src: str) -> set[str]:
    return set(re.findall(r"\b([A-Z])\b(?=\s*(?:\[|-->|\Z))", src))


def _edges(src: str) -> int:
    return len(re.findall(r"-->|---|-\.->|==>", src))


def test_space_before_class_marker_is_removed():
    assert df.repair('D["a b"] :::accent') == 'D["a b"]:::accent'
    assert df.repair("E(x)   :::hot") == "E(x):::hot"
    assert df.repair('D["a"]:::accent') == 'D["a"]:::accent'


def test_long_lr_chain_becomes_td():
    out = df.fit(LR5)
    assert out.split("\n")[0] == "flowchart TD"
    assert _edges(out) == _edges(LR5)


def test_fan_out_keeps_direction():
    fan = ('flowchart LR\n    A["x"] --> B["a"]\n    A --> C["b"]\n    A --> D["c"]\n    D --> E["e"]')
    assert df.fit(fan).startswith("flowchart LR")
    two = ('flowchart LR\n    A["x"] --> B["a"]\n    A --> C["b"]\n    C --> D["c"]\n    D --> E["e"]')
    assert df.fit(two).startswith("flowchart TD")


def test_bare_node_id_class_marker_is_repaired():
    assert df.repair("A :::accent") == "A:::accent"


def test_graph_keyword_and_rl_are_switched():
    assert df.fit(LR5.replace("flowchart LR", "graph RL")).startswith("graph TD")


def test_short_chain_keeps_direction():
    short = 'flowchart LR\n    A["x"] --> B["y"]\n    B --> C["z"]'
    assert df.fit(short).startswith("flowchart LR")


def test_subgraph_keeps_direction():
    sub = LR5 + '\n    subgraph S["group"]\n    C\n    end'
    assert df.fit(sub).startswith("flowchart LR")


def test_td_is_untouched():
    td = LR5.replace("LR", "TD")
    assert df.fit(td) == td


def test_other_diagram_types_only_get_syntax_repair():
    seq = "sequenceDiagram\n    A->>B: hi"
    assert df.fit(seq) == seq
    mind = "mindmap\n  root((x))"
    assert df.fit(mind) == mind


def test_init_directive_and_front_matter_are_skipped_to_find_the_header():
    src = '%%{init: {"theme": "base"}}%%\n' + LR5
    assert df.fit(src).split("\n")[1] == "flowchart TD"
    fm = "---\ntitle: t\n---\n" + LR5
    assert df.fit(fm).split("\n")[3] == "flowchart TD"


def test_long_labels_wrap_and_short_ones_do_not():
    src = 'flowchart TD\n    A["A very long label that keeps going and going"] --> B["Short"]'
    out = df.fit(src)
    assert "<br/>" in out and 'B["Short"]' in out
    assert all(len(part) <= 30 for part in re.search(r'A\["(.*?)"\]', out).group(1).split("<br/>"))
    assert "".join(re.search(r'A\["(.*?)"\]', out).group(1).split("<br/>")).replace(" ", "") == \
        "Averylonglabelthatkeepsgoingandgoing"


def test_existing_line_breaks_are_kept():
    src = 'flowchart TD\n    A["one very long label here<br/>and a second line that is long"] --> B'
    assert df.fit(src) == src


def test_idempotent():
    for s in (LR5, 'D["a b"] :::accent', 'flowchart TD\n    A["A very long label that keeps going and going"] --> B'):
        once = df.fit(s)
        assert df.fit(once) == once


def test_nodes_and_edges_are_preserved():
    src = LR5.replace('"Start"', '"Start of a very long first label with many words"')
    out = df.fit(src)
    assert _ids(out) == _ids(src) and _edges(out) == _edges(src)
    assert len(out.split("\n")) == len(src.split("\n"))


def test_never_raises_and_empty_passes_through():
    assert df.fit("") == ""
    assert df.fit("   ") == "   "
    assert df.fit(None) is None


# ── intake points ────────────────────────────────────────────────────────────

def test_normalize_mermaid_fits_generated_diagrams():
    import main
    out = main._normalize_mermaid(LR5)
    assert out.split("\n")[0] == "flowchart TD"


def test_pasted_clip_diagram_is_stored_fitted(monkeypatch):
    import main
    monkeypatch.setattr(main, "_processed_clip_match", lambda sig: None)
    monkeypatch.setattr(main.vault_reader, "list_concept_pages", lambda: [])
    monkeypatch.setattr(main, "_extract_with_sonnet", lambda *a, **k: {
        "title": "T", "key_concepts": [], "summary": ["a claim"], "suggested_page": "p",
        "suggested_wikilinks": [], "tags": [], "references": [], "diagram": ""})
    monkeypatch.setattr(main, "_curation_fields", lambda d: {})
    monkeypatch.setattr(main, "_attach_report", lambda item: item.setdefault("conflict_report", {"band": "distinct"}))
    stored = []
    monkeypatch.setattr(main.queue_manager, "enqueue", stored.append)
    md = "# Clip\n\n- one\n- two\n\n```mermaid\n" + LR5 + "\n```\n"
    main._stage_markdown_clip(md)
    assert stored[0]["diagram"].split("\n")[0] == "flowchart TD"
