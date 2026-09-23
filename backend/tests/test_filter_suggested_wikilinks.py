import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


@pytest.fixture(autouse=True)
def isolated_vault(monkeypatch, tmp_path):
    # Slugs resolve through the vault's alias file; keep the real vault out of it.
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))


def filter_links(suggested, existing=(), title="Queue backpressure", summary=(), concepts=(), max_links=6):
    return main._filter_suggested_wikilinks(
        list(suggested), title, list(summary), list(concepts), list(existing), max_links=max_links
    )


def test_empty_suggestions():
    assert filter_links([]) == []


def test_empty_scope_rejects_everything():
    assert filter_links(["queue-backpressure"], title="", summary=[], concepts=[]) == []


def test_existing_page_needs_one_overlapping_word():
    assert filter_links(["queue-theory"], existing=["queue-theory"]) == ["queue-theory"]


def test_new_multiword_page_needs_two_overlapping_words():
    # "queue-theory" shares only "queue" with the scope and isn't an existing page.
    assert filter_links(["queue-theory"]) == []
    assert filter_links(["queue-backpressure"]) == ["queue-backpressure"]


def test_new_single_word_page_needs_one_overlapping_word():
    assert filter_links(["backpressure"]) == ["backpressure"]
    assert filter_links(["kubernetes"]) == []


def test_scope_includes_summary_and_concepts():
    # "load" comes from the concepts, "shedding" from the summary; neither is in the title.
    assert filter_links(["load-shedding"], summary=["Start shedding requests"], concepts=["Load balancing"]) == ["load-shedding"]


def test_matches_whole_words_only():
    # No stemming: "shed" in scope does not match "shedding" in the slug.
    assert filter_links(["load-shedding"], summary=["Shed load when full"]) == []


def test_normalizes_and_dedupes_including_aliases():
    result = filter_links(["Queue Backpressure", "queue-backpressure", "Agent", "agents"], title="Queue backpressure for agents")
    assert result == ["queue-backpressure", "agents"]


def test_caps_at_max_links():
    words = ["alpha", "beta", "gamma", "delta"]
    result = filter_links(words, title=" ".join(words), max_links=2)
    assert result == ["alpha", "beta"]
