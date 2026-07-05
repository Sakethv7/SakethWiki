from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import memory_store
import preference_memory
import active_review
import consolidation
import vault_reader


def _write_page(path: Path, title: str, tags: str, body: str, extra_frontmatter: str = "") -> None:
    extra = f"{extra_frontmatter.rstrip()}\n" if extra_frontmatter else ""
    path.write_text(
        f"""---
title: "{title}"
tags: [{tags}]
last_updated: 2026-07-04
entry_count: 1
{extra}---
# {title}

{body}
""",
        encoding="utf-8",
    )


def test_memory_index_and_search(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / "_wiki" / "cs").mkdir(parents=True)
    (vault / "_wiki" / "insights").mkdir(parents=True)
    (vault / "_wiki" / "open-threads").mkdir(parents=True)
    (vault / "_wiki" / "meta").mkdir(parents=True)

    monkeypatch.setenv("VAULT_PATH", str(vault))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_COMPAT_API_KEY", raising=False)

    _write_page(
        vault / "_wiki" / "cs" / "kv-cache.md",
        "KV Cache",
        "LLM, KVCache",
        """> **Current understanding** 🟡
> KV cache stores attention keys and values so decoding reuses prior work instead of recomputing attention from scratch.

## Decoder optimization · 2026-07-04
- KV cache shifts inference cost from repeated FLOPs toward memory bandwidth and VRAM pressure.
""",
    )
    _write_page(
        vault / "_wiki" / "insights" / "gpu-bottlenecks.md",
        "GPU Bottlenecks",
        "Systems, Inference",
        """GPU throughput often collapses on memory-bound decode workloads before tensor cores saturate.""",
    )

    first_sync = memory_store.sync_index()
    assert first_sync["pages_seen"] == 2

    hits = memory_store.search("Why does kv cache make decoding memory bandwidth bound?", limit=3)
    assert hits, "expected at least one memory hit"
    assert hits[0]["page_name"] == "kv-cache"
    assert any("memory bandwidth" in snippet.lower() for snippet in hits[0]["snippets"])

    kv_page = vault / "_wiki" / "cs" / "kv-cache.md"
    updated = kv_page.read_text(encoding="utf-8") + "\n- Paged attention reduces fragmentation when KV cache grows.\n"
    kv_page.write_text(updated, encoding="utf-8")

    second_sync = memory_store.sync_index()
    assert second_sync["indexed"] >= 1

    hits_after_edit = memory_store.search("What do I know about paged attention?", limit=3)
    assert hits_after_edit[0]["page_name"] == "kv-cache"

    (vault / "_wiki" / "insights" / "gpu-bottlenecks.md").unlink()
    third_sync = memory_store.sync_index()
    assert third_sync["removed"] == 1


def test_alias_resolution_indexes_canonical_page(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / "_wiki" / "cs").mkdir(parents=True)
    (vault / "_wiki" / "meta").mkdir(parents=True)

    monkeypatch.setenv("VAULT_PATH", str(vault))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_COMPAT_API_KEY", raising=False)

    _write_page(
        vault / "_wiki" / "cs" / "rag.md",
        "RAG",
        "RAG, LLM",
        """Retrieval augmented generation grounds generation in retrieved context before answering.""",
    )
    _write_page(
        vault / "_wiki" / "cs" / "retrieval-augmented-generation.md",
        "Retrieval Augmented Generation",
        "RAG, LLM",
        """Duplicate alias page that should not compete with the canonical page.""",
    )

    sync = memory_store.sync_index()
    assert sync["pages_seen"] == 1

    hits = memory_store.search("retrieval augmented generation", limit=3)
    assert hits
    assert hits[0]["page_name"] == "rag"

    assert vault_reader.read_page("retrieval augmented generation") is not None
    assert vault_reader.parse_concept_page("retrieval-augmented-generation")["name"] == "rag"


def test_find_relevant_pages_scores_all_files(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / "_wiki" / "cs").mkdir(parents=True)

    monkeypatch.setenv("VAULT_PATH", str(vault))

    _write_page(
        vault / "_wiki" / "cs" / "aaa-target.md",
        "AAA Target",
        "Systems",
        "This page contains the distinctive phrase factory calibration loop.",
    )
    _write_page(
        vault / "_wiki" / "cs" / "zzz-other.md",
        "ZZZ Other",
        "Systems",
        "This page is unrelated.",
    )

    hits = vault_reader.find_relevant_pages("factory calibration loop", max_pages=2)
    assert hits[0] == "aaa-target"


def test_preference_memory_learns_from_corrections(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / "_wiki" / "meta").mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))

    preference_memory.record_approval_trace(
        {
            "approved": True,
            "url": "https://example.com/rag",
            "suggested_page": "language-model-memory",
            "final_page": "rag",
            "page_corrected": True,
            "tags_suggested": ["Agentic"],
            "tags_final": ["Agents", "RAG"],
            "tags_corrected": True,
        }
    )
    preference_memory.record_approval_trace(
        {
            "approved": False,
            "title": "Rejected page",
            "suggested_page": "random-slug",
            "tags_suggested": ["Product"],
        }
    )

    data = preference_memory.load()
    assert data["page_corrections"]["language-model-memory"]["rag"]["count"] == 1
    assert data["tag_corrections"]["Agentic"]["Agents"]["count"] == 1
    assert data["tag_corrections"]["Agentic"]["RAG"]["count"] == 1
    assert data["rejected_pages"]["random-slug"]["count"] == 1

    assert preference_memory.preferred_page("language model memory") == "rag"
    assert preference_memory.preferred_tags(["Agentic"]) == ["RAG"]

    hints = preference_memory.prompt_hints()
    assert "Prefer page `rag` instead of `language-model-memory`" in hints
    assert "Prefer tag `RAG` instead of `Agentic`" in hints
    assert "Be cautious about creating or using page `random-slug`" in hints


def test_active_review_prioritizes_weak_orphaned_pages(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / "_wiki" / "cs").mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))

    _write_page(
        vault / "_wiki" / "cs" / "weak-concept.md",
        "Weak Concept",
        "Learning",
        "Thin note. [!warning] Contradiction needs review.",
        extra_frontmatter="understanding_maturity: 25",
    )
    _write_page(
        vault / "_wiki" / "cs" / "mature-concept.md",
        "Mature Concept",
        "Learning",
        "This is a stronger concept page with enough body text to avoid thin-page treatment. "
        "It also links to [[weak-concept]] so the weak page has at least one inbound signal.",
        extra_frontmatter="understanding_maturity: 85",
    )

    queue = active_review.build_queue(limit=10)
    assert queue[0]["name"] == "weak-concept"
    assert queue[0]["priority"] == "high"
    assert "unresolved conflict" in " ".join(queue[0]["reasons"])
    assert queue[0]["signals"]["maturity"] == 25


def test_consolidation_candidates_are_conservative(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / "_wiki" / "cs").mkdir(parents=True)
    (vault / "_wiki" / "meta").mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))

    _write_page(
        vault / "_wiki" / "cs" / "rag.md",
        "RAG",
        "RAG",
        "Retrieval augmented generation grounds answers in retrieved context.",
    )
    _write_page(
        vault / "_wiki" / "cs" / "retrieval-augmented-generation.md",
        "Retrieval Augmented Generation",
        "RAG",
        "Retrieval augmented generation uses retrieval before generation.",
    )
    _write_page(
        vault / "_wiki" / "cs" / "binary-search.md",
        "Binary Search",
        "BinarySearch",
        "Binary search halves a sorted search interval.",
    )

    candidates = consolidation.find_candidates()
    alias_candidate = next(c for c in candidates if c["source"] == "retrieval-augmented-generation")
    assert alias_candidate["target"] == "rag"
    assert alias_candidate["safe_auto"] is True
    assert alias_candidate["confidence"] == "high"

    unsafe = consolidation.validate_pair("binary-search", "rag")
    assert unsafe["safe_auto"] is False
