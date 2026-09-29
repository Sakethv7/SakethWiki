import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wiki_writer


@pytest.fixture(autouse=True)
def isolated_vault(monkeypatch, tmp_path):
    # Wikilinks resolve through the vault's alias file; keep the real vault out of it.
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))


def test_minimal_item():
    section = wiki_writer._format_section({})
    today = datetime.now().strftime("%Y-%m-%d")

    assert section.startswith(f"## [Untitled]() · {today}\n\n")
    assert "**Insight map**" not in section
    assert "Related:" not in section
    assert "**Links:**" not in section
    assert "```mermaid" not in section
    assert section.endswith("\n---")


def test_full_item():
    item = {
        "url": "https://example.com/a",
        "title": "Backpressure",
        "summary": ["Queues need limits — or memory grows without bound", "I learned that slow consumers push back"],
        "suggested_wikilinks": ["Queue Theory", "Agent"],
        "references": ["https://example.com/ref", "not-a-url", ""],
        "diagram": "  graph TD; A-->B  ",
    }

    section = wiki_writer._format_section(item)

    assert section.startswith("## [Backpressure](https://example.com/a) · ")
    assert "| Insight | Why it matters |" in section
    assert "| Queues need limits | or memory grows without bound |" in section
    assert "- Queues need limits — or memory grows without bound\n- I learned that slow consumers push back" in section
    # Key insight is the last bullet with the "I learned that" opener stripped.
    assert "**Key insight:** Slow consumers push back" in section
    # Wikilinks are kebab-cased and alias-resolved.
    assert "Related: [[queue-theory]], [[agents]]" in section
    # Only http references are linked.
    assert "**Links:** [↗](https://example.com/ref)\n" in section
    assert "\n```mermaid\ngraph TD; A-->B\n```\n" in section


def test_single_bullet_has_no_table():
    section = wiki_writer._format_section({"summary": ["One point only"]})
    assert "**Insight map**" not in section
    assert "**Key insight:** One point only" in section


def test_pipes_in_summary_do_not_break_table():
    section = wiki_writer._format_section({"summary": ["a | b: c", "d: e | f"]})
    assert "| a \\| b | c |" in section
    assert "| d | e \\| f |" in section
