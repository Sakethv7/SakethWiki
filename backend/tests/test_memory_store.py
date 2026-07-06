from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import memory_store
import preference_memory
import active_review
import consolidation
import eval_harness
import identity
import llm_client
import main
import system_loop
import telemetry
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
    assert data["page_corrections"]["language-model-memory"]["rag"]["status"] == "candidate"
    assert data["tag_corrections"]["Agentic"]["Agents"]["count"] == 1
    assert data["tag_corrections"]["Agentic"]["RAG"]["count"] == 1
    assert data["rejected_pages"]["random-slug"]["count"] == 1

    assert preference_memory.preferred_page("language model memory") == "language-model-memory"
    assert preference_memory.preferred_tags(["Agentic"]) == ["Agentic"]
    assert any(item["type"] == "page_correction" for item in preference_memory.review_candidates())

    preference_memory.set_preference_status("page_correction", "language-model-memory", "rag", "active")
    preference_memory.set_preference_status("tag_correction", "Agentic", "RAG", "active")
    preference_memory.set_preference_status("rejected_page", "random-slug", "random-slug", "active")

    assert preference_memory.preferred_page("language model memory") == "rag"
    assert preference_memory.preferred_tags(["Agentic"]) == ["RAG"]

    hints = preference_memory.prompt_hints()
    assert "Prefer page `rag` instead of `language-model-memory`" in hints
    assert "Prefer tag `RAG` instead of `Agentic`" in hints
    assert "Be cautious about creating or using page `random-slug`" in hints


def test_preference_memory_auto_activates_repeated_evidence(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / "_wiki" / "meta").mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))

    trace = {
        "approved": True,
        "url": "https://example.com/rag",
        "suggested_page": "language-model-memory",
        "final_page": "rag",
        "page_corrected": True,
        "tags_suggested": ["Agentic"],
        "tags_final": ["RAG"],
        "tags_corrected": True,
    }
    preference_memory.record_approval_trace(trace)
    preference_memory.record_approval_trace(trace)

    data = preference_memory.load()
    assert data["page_corrections"]["language-model-memory"]["rag"]["status"] == "active"
    assert data["tag_corrections"]["Agentic"]["RAG"]["status"] == "active"
    assert preference_memory.preferred_page("language model memory") == "rag"
    assert preference_memory.preferred_tags(["Agentic"]) == ["RAG"]


def test_preference_review_can_keep_candidate_from_auto_applying(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / "_wiki" / "meta").mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))

    trace = {
        "approved": True,
        "url": "https://example.com/rag",
        "suggested_page": "language-model-memory",
        "final_page": "rag",
        "page_corrected": True,
    }
    preference_memory.record_approval_trace(trace)
    preference_memory.record_approval_trace(trace)
    preference_memory.set_preference_status("page_correction", "language-model-memory", "rag", "candidate")

    assert preference_memory.load()["page_corrections"]["language-model-memory"]["rag"]["status"] == "candidate"
    assert preference_memory.preferred_page("language model memory") == "language-model-memory"


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


def test_expand_notes_parser_accepts_markdown_and_json():
    assert main._parse_bullet_array('["A new point.", "Another point."]') == [
        "A new point.",
        "Another point.",
    ]
    assert main._parse_bullet_array(
        "```json\n[\"Fenced point.\"]\n```"
    ) == ["Fenced point."]
    assert main._parse_bullet_array(
        "- Recovery is CPU-intensive.\n- Parity count changes decode cost."
    ) == [
        "Recovery is CPU-intensive.",
        "Parity count changes decode cost.",
    ]


def test_telemetry_generates_inference_report(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / "_wiki" / "meta").mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))

    telemetry.log_llm_call(
        {
            "task": "INGEST_EXTRACT",
            "provider": "openai_compat",
            "requested_provider": "openai_compat",
            "model": "gemini-2.5-flash",
            "duration_ms": 1200,
            "input_chars": 6000,
            "output_chars": 900,
            "max_tokens": 1200,
            "expect_json": True,
            "contract_ok": True,
            "fallback_used": False,
            "error": None,
        }
    )
    telemetry.log_context_event(
        "ingest_context",
        {
            "task": "ingest_extract",
            "source_url": "https://example.com/long",
            "source_chars_total": 10000,
            "source_chars_used": 4000,
            "source_coverage_ratio": 0.4,
        },
    )

    report = telemetry.generate_inference_report()
    path = Path(report["path"])
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "Inference Engineering Report" in text
    assert "INGEST_EXTRACT" in text
    assert "40.0%" in text


def test_system_loop_routes_repeated_preferences(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / "_wiki" / "meta").mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))

    trace = {
        "approved": True,
        "url": "https://example.com/agents",
        "suggested_page": "agentic-ai",
        "final_page": "agents",
        "page_corrected": True,
    }
    preference_memory.record_approval_trace(trace)
    preference_memory.record_approval_trace(trace)

    result = system_loop.run_system_loop(auto_apply=True)
    assert result["success"] is True
    assert preference_memory.preferred_page("agentic ai") == "agents"
    assert Path(result["report_path"]).exists()


def test_system_loop_can_set_runtime_route_override(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / "_wiki" / "meta").mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))

    for _ in range(20):
        telemetry.log_llm_call(
            {
                "task": "INGEST_EXTRACT",
                "provider": "openai_compat",
                "requested_provider": "openai_compat",
                "model": "gemini-2.5-flash",
                "duration_ms": 1000,
                "input_chars": 5000,
                "output_chars": 800,
                "max_tokens": 1200,
                "expect_json": True,
                "contract_ok": True,
                "fallback_used": True,
                "error": None,
            }
        )

    result = system_loop.run_system_loop(auto_apply=True)
    overrides = system_loop.load_runtime_overrides()
    assert overrides["routes"]["INGEST_EXTRACT"]["provider"] == "anthropic"
    assert any(action["action"] == "set_runtime_route_override" for action in result["actions"])


def test_action_candidate_approval_applies_runtime_setting(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / "_wiki" / "meta").mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))
    for _ in range(5):
        telemetry.log_context_event(
            "chat_context",
            {
                "query": "why did retrieval drop chunks",
                "retrieved_chunks_total": 5,
                "retrieved_chunks_used": 3,
                "retrieved_chunks_dropped": 2,
                "context_chars_used": 6000,
                "context_chars_dropped": 1200,
                "context_budget": 6000,
            },
        )

    candidate = system_loop.upsert_action_candidate(
        {
            "risk": "medium",
            "action": "increase_chat_context_budget",
            "target": "chat_answer",
            "title": "Increase chat context budget",
            "reason": "chat dropped chunks",
            "current_state": {"chat_context_budget": 6000},
            "proposed_change": {"chat_context_budget": 9000},
        }
    )
    approved = system_loop.approve_action_candidate(candidate["id"])
    settings = system_loop.load_runtime_settings()

    assert approved["status"] == "applied"
    assert approved["eval_status"] == "passed"
    assert settings["settings"]["chat_context_budget"] == 9000


def test_budget_candidate_eval_fails_without_enough_evidence(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / "_wiki" / "meta").mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))
    telemetry.log_context_event(
        "chat_context",
        {
            "query": "one dropped context event",
            "retrieved_chunks_total": 5,
            "retrieved_chunks_used": 3,
            "retrieved_chunks_dropped": 2,
            "context_chars_used": 6000,
            "context_chars_dropped": 1200,
            "context_budget": 6000,
        },
    )
    candidate = system_loop.upsert_action_candidate(
        {
            "risk": "medium",
            "action": "increase_chat_context_budget",
            "target": "chat_answer",
            "title": "Increase chat context budget",
            "reason": "chat dropped chunks",
            "current_state": {"chat_context_budget": 6000},
            "proposed_change": {"chat_context_budget": 9000},
        }
    )

    approved = system_loop.approve_action_candidate(candidate["id"])

    assert approved["status"] == "eval_failed"
    assert system_loop.load_runtime_settings()["settings"] == {}


def test_alias_candidate_adds_alias(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / "_wiki" / "meta").mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))

    candidate = system_loop.upsert_action_candidate(
        {
            "risk": "medium",
            "action": "add_alias",
            "target": "rag",
            "title": "Add RAG alias",
            "reason": "retrieval missed rag",
            "proposed_change": {"alias": "retrieval grounding", "canonical": "rag"},
        }
    )
    approved = system_loop.approve_action_candidate(candidate["id"])

    assert approved["status"] == "applied"
    assert identity.resolve_slug("retrieval grounding") == "rag"


def test_exclude_eval_case_candidate_records_exclusion(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / "_wiki" / "meta").mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))

    candidate = system_loop.upsert_action_candidate(
        {
            "risk": "medium",
            "action": "exclude_eval_case",
            "target": "trace-2",
            "title": "Exclude noisy eval case",
            "reason": "bad trace",
            "proposed_change": {"case_id": "trace-2"},
        }
    )
    approved = system_loop.approve_action_candidate(candidate["id"])

    assert approved["status"] == "applied"
    assert "trace-2" in eval_harness.load_eval_exclusions()["excluded_cases"]


def test_route_eval_findings_stages_alias_and_exclusion_candidates(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / "_wiki" / "meta").mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))

    actions = system_loop.route_eval_findings(
        {
            "retrieval": {
                "failures": [
                    {
                        "case_id": "trace-7",
                        "expected": "rag",
                        "got": ["agents"],
                        "query": "retrieval grounding",
                    }
                ]
            }
        }
    )
    candidates = system_loop.list_action_candidates()

    assert any(action["action"] == "stage_add_alias_candidate" for action in actions)
    assert any(c["action"] == "add_alias" for c in candidates)
    assert any(c["action"] == "exclude_eval_case" for c in candidates)


def test_curated_eval_cases_are_loaded(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / "_wiki" / "meta").mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))

    eval_harness.eval_cases_path().write_text(
        """{
  "cases": [
    {"id": "curated-rag", "query": "retrieval grounding", "expected_page": "rag", "tags": ["RAG"]}
  ]
}
""",
        encoding="utf-8",
    )

    cases = eval_harness.load_curated_eval_cases()
    assert cases[0]["id"] == "curated-rag"
    assert cases[0]["expected_page"] == "rag"


def test_ingest_budget_candidate_applies_after_replay_gate(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / "_wiki" / "meta").mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))
    for _ in range(5):
        telemetry.log_context_event(
            "ingest_context",
            {
                "task": "ingest_extract",
                "source_chars_total": 10000,
                "source_chars_used": 4000,
                "source_coverage_ratio": 0.4,
            },
        )

    candidate = system_loop.upsert_action_candidate(
        {
            "risk": "medium",
            "action": "increase_ingest_source_budget",
            "target": "ingest_extract",
            "title": "Increase ingest budget",
            "reason": "low source coverage",
            "current_state": {"ingest_source_budget": 4000},
            "proposed_change": {"ingest_source_budget": 8000},
        }
    )
    approved = system_loop.approve_action_candidate(candidate["id"])

    assert approved["status"] == "applied"
    assert system_loop.load_runtime_settings()["settings"]["ingest_source_budget"] == 8000


def test_high_risk_review_and_consolidation_handlers(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / "_wiki" / "meta").mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))

    review = system_loop.upsert_action_candidate(
        {
            "risk": "high",
            "action": "queue_page_review",
            "target": "rag",
            "title": "Review RAG",
            "reason": "trace critic found repeated corrections",
            "proposed_change": {"page": "rag"},
            "requires_eval": False,
            "requires_approval": True,
        }
    )
    merged = system_loop.upsert_action_candidate(
        {
            "risk": "high",
            "action": "create_consolidation_candidate",
            "target": "rag-systems->rag",
            "title": "Consider consolidation",
            "reason": "duplicate concepts",
            "proposed_change": {"source": "rag-systems", "target": "rag"},
            "requires_eval": False,
            "requires_approval": True,
        }
    )

    assert system_loop.approve_action_candidate(review["id"])["status"] == "applied"
    assert system_loop.approve_action_candidate(merged["id"])["status"] == "applied"
    assert system_loop.review_requests_path().exists()
    assert system_loop.consolidation_requests_path().exists()


def test_trace_critic_drops_malformed_candidates(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / "_wiki" / "meta").mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))
    traces_path = vault / "_wiki" / "meta" / "traces.jsonl"
    for i in range(5):
        traces_path.write_text(
            (traces_path.read_text(encoding="utf-8") if traces_path.exists() else "")
            + f'{{"approved": true, "title": "Trace {i}", "suggested_page": "a", "final_page": "b"}}\n',
            encoding="utf-8",
        )

    def fake_complete(*args, **kwargs):
        return """{
  "quality_summary": "malformed candidates should not enter the queue",
  "bad_trace_cases": [],
  "good_eval_cases": [],
  "action_candidates": [
    {"action": "add_alias", "risk": "medium", "proposed_change": {"canonical": "rag"}},
    {"action": "queue_page_review", "risk": "high", "proposed_change": {}},
    {"action": "create_consolidation_candidate", "risk": "high", "proposed_change": {"target": "rag"}}
  ]
}"""

    monkeypatch.setattr(llm_client, "complete", fake_complete)
    result = system_loop.run_trace_critic()

    assert result["ran"] is True
    assert result["staged"] == []
    assert system_loop.list_action_candidates() == []
