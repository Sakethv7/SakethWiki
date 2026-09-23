from pathlib import Path
from datetime import datetime, timedelta
import asyncio
import base64
import json
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


def test_telemetry_summarizes_llm_cost(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / "_wiki" / "meta").mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))

    telemetry.log_llm_call(
        {
            "task": "INGEST_EXTRACT",
            "provider": "anthropic",
            "requested_provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "effective_provider": "anthropic",
            "effective_model": "claude-sonnet-4-6",
            "duration_ms": 25000,
            "input_chars": 12000,
            "output_chars": 2000,
            "input_tokens": 3000,
            "output_tokens": 500,
            "total_tokens": 3500,
            "cost_usd": 0.0165,
            "cost_per_token_usd": 0.0000047143,
            "cost_estimated": False,
            "max_tokens": 1500,
            "expect_json": True,
            "contract_ok": True,
            "fallback_used": False,
            "error": None,
        }
    )

    summary = telemetry.summarize_llm_calls()

    assert summary["total_tokens"] == 3500
    assert summary["total_cost_usd"] == 0.0165
    assert summary["by_task"]["INGEST_EXTRACT"]["total_cost_usd"] == 0.0165
    assert summary["by_task"]["INGEST_EXTRACT"]["cost_per_token_usd"] == 0.0000047143
    assert summary["recent_expensive_calls"][0]["task"] == "INGEST_EXTRACT"


def test_telemetry_backfills_legacy_char_usage(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / "_wiki" / "meta").mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))

    telemetry.log_llm_call(
        {
            "task": "IMAGE_CAPTION",
            "provider": "openai_compat",
            "requested_provider": "openai_compat",
            "model": "gemini-2.5-flash",
            "duration_ms": 3200,
            "input_chars": 4000,
            "output_chars": 800,
            "expect_json": True,
            "contract_ok": False,
            "fallback_used": False,
            "error": "LLM contract failed",
        }
    )

    summary = telemetry.summarize_llm_calls()
    task = summary["by_task"]["IMAGE_CAPTION"]

    assert summary["total_tokens"] == 1200
    assert summary["total_cost_usd"] > 0
    assert task["estimated_cost_rate"] == 1.0
    assert task["contract_failure_rate"] == 1.0
    assert summary["recent_expensive_calls"][0]["cost_estimated"] is True


def test_image_caption_response_parser_handles_json_and_prose():
    parsed = main._parse_image_caption_response(
        '```json\n{"slug": "Latency Dashboard", "caption": "A dashboard showing ingest latency."}\n```',
        index=0,
    )
    assert parsed == {
        "slug": "latency-dashboard",
        "caption": "A dashboard showing ingest latency.",
    }

    prose = main._parse_image_caption_response("Screenshot of operation telemetry panels.", index=1)
    assert prose["slug"] == "screenshot-of-operation-telemetry-panels"
    assert prose["caption"] == "Screenshot of operation telemetry panels."

    empty = main._parse_image_caption_response("", index=2)
    assert empty == {"slug": "image-3", "caption": ""}


def test_store_image_skips_large_optional_caption(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / "_wiki" / "meta").mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))
    monkeypatch.setenv("IMAGE_CAPTION_MAX_BASE64_CHARS", "10")

    def fail_if_called(**kwargs):
        raise AssertionError("large optional image caption should not call LLM")

    monkeypatch.setattr(llm_client, "complete", fail_if_called)
    image_data = base64.b64encode(b"large-enough-image-bytes").decode("ascii")

    result = asyncio.run(main.store_image(main.IngestRequest(images=[{"data": image_data, "mediaType": "image/png"}])))
    context = telemetry.summarize_context_events()

    assert result["saved"][0]["filename"].endswith("image-1.png")
    assert context["store_image_latency_events"] == 1
    assert context["recent_store_image_latency"][0]["caption_skipped_count"] == 1


def test_chat_notes_write_trace_and_telemetry(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / "_wiki" / "meta").mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))

    result = asyncio.run(main.add_chat_note(main.ChatNoteRequest(
        note_type="nuance",
        note="A weaker judge is fine for first-pass triage, not final authority.",
        question="Can Llama 3.2 judge Llama 3.3?",
        answer_excerpt="Yes, but capability matters.",
        pages_read=["llm-as-judge", "agent-evaluation-methods-evals"],
        sources=["_wiki/cs/llm-as-judge.md"],
    )))

    traces_path = vault / "_wiki" / "meta" / "traces.jsonl"
    trace = json.loads(traces_path.read_text(encoding="utf-8").splitlines()[0])
    summary = telemetry.summarize_context_events()

    assert result["success"] is True
    assert trace["event_type"] == "chat_note"
    assert trace["note_type"] == "nuance"
    assert "approved" not in trace
    assert summary["chat_note_events"] == 1
    assert summary["chat_note_type_counts"]["nuance"] == 1
    assert summary["chat_note_top_pages"][0]["page"] == "agent-evaluation-methods-evals"


def test_chat_notes_reject_unknown_type(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / "_wiki" / "meta").mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))

    try:
        asyncio.run(main.add_chat_note(main.ChatNoteRequest(note_type="idea", note="too vague")))
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 400
    else:
        raise AssertionError("unknown chat note type should fail")


def test_system_level_eval_reports_runtime_gates(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / "_wiki" / "meta").mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))

    main._append_trace({
        "ts": "2026-07-13T10:00:00",
        "approved": True,
        "final_page": "rag",
        "source_type": "text",
    })
    telemetry.log_context_event(
        "chat_context",
        {
            "query": "what do I know about rag",
            "retrieved_chunks_dropped": 2,
            "context_chars_used": 6000,
            "context_chars_dropped": 1200,
        },
    )
    telemetry.log_system_action({
        "action": "flag_chat_context_drops",
        "reason": "test dropped chunks",
        "applied": False,
    })
    system_loop.upsert_action_candidate({
        "risk": "medium",
        "action": "increase_chat_context_budget",
        "target": "chat_answer",
        "reason": "test pending action",
        "proposed_change": {"chat_context_budget": 9000},
        "current_state": {"chat_context_budget": 6000},
    })

    result = eval_harness.run_system_level_eval()

    assert result["suite"] == "system_level"
    assert result["passed"] is True
    assert result["trace_counts"]["approved"] == 1
    assert result["telemetry_counts"]["chat_events"] == 1
    assert result["telemetry_counts"]["pending_actions"] == 1
    assert any(gate["name"] == "dropped_chat_context" and not gate["passed"] for gate in result["gates"])
    assert result["recent_system_actions"][-1]["action"] == "flag_chat_context_drops"


def test_llm_client_complete_runs_on_python39(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / "_wiki" / "meta").mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")

    def fake_complete(**kwargs):
        return llm_client.CompletionResult("ok", {"input_tokens": 2, "output_tokens": 1, "source": "provider"})

    monkeypatch.setattr(llm_client, "_anthropic_complete", fake_complete)

    result = llm_client.complete(
        task="chat_answer",
        model=None,
        max_tokens=10,
        messages=[{"role": "user", "content": "ping"}],
    )

    assert result == "ok"
    assert telemetry.summarize_llm_calls()["total_calls"] == 1


def test_dashboard_stats_distinguish_new_touched_and_rejected():
    now = datetime(2026, 7, 8, 12, 0, 0)
    traces = [
        {
            "ts": (now - timedelta(days=2)).isoformat(),
            "approved": True,
            "final_page": "new-page",
            "tags_final": ["Systems"],
            "source_type": "clip_markdown",
        },
        {
            "ts": (now - timedelta(days=1)).isoformat(),
            "approved": True,
            "final_page": "old-page",
            "tags_final": ["Systems", "Agents"],
            "source_type": "text",
        },
        {
            "ts": (now - timedelta(days=20)).isoformat(),
            "approved": True,
            "final_page": "old-page",
            "tags_final": ["Agents"],
            "source_type": "lecture",
        },
        {
            "ts": (now - timedelta(days=3)).isoformat(),
            "approved": False,
            "suggested_page": "rejected-page",
            "source_type": "text",
        },
        {
            "ts": (now - timedelta(days=80)).isoformat(),
            "approved": True,
            "final_page": "heatmap-only",
            "tags_final": ["Archive"],
            "source_type": "url",
        },
    ]

    stats = main._dashboard_stats_from_traces(traces, now=now, period_days=30, heatmap_days=112)

    assert stats["total_approved"] == 3
    assert stats["total_rejected"] == 1
    assert stats["approval_rate"] == 0.75
    assert stats["unique_concepts"] == 2
    assert stats["concepts_touched_this_week"] == 2
    assert stats["new_concepts_this_week"] == 1
    assert stats["activity_by_date"][(now - timedelta(days=80)).date().isoformat()] == 1
    assert {row["source"]: row["count"] for row in stats["top_sources"]} == {
        "clip": 1,
        "lecture": 1,
        "text": 1,
    }
    assert stats["top_tags"][0] == {"tag": "Agents", "count": 2}


def test_review_due_handles_string_maturity(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / "_wiki" / "cs").mkdir(parents=True)
    (vault / "_wiki" / "meta").mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))

    _write_page(
        vault / "_wiki" / "cs" / "quoted-maturity.md",
        "Quoted Maturity",
        "Learning",
        "A page with string maturity.",
        extra_frontmatter='understanding_maturity: "85"',
    )
    _write_page(
        vault / "_wiki" / "cs" / "weak-string-maturity.md",
        "Weak String Maturity",
        "Learning",
        "A page with low string maturity.",
        extra_frontmatter='understanding_maturity: "25"',
    )

    result = asyncio.run(main.review_due())

    names = {row["name"] for row in result["due"]}
    assert "quoted-maturity" not in names
    assert "weak-string-maturity" in names


def test_telemetry_summarizes_ingest_latency(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / "_wiki" / "meta").mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))

    telemetry.log_context_event(
        "ingest_latency",
        {
            "source_type": "lecture",
            "image_count": 3,
            "total_ms": 42000,
            "stage_ms": {
                "image_uncertainty_extract": 9000,
                "image_gap_search": 5000,
                "vision_extract": 26000,
                "queue_stage": 20,
            },
        },
    )
    telemetry.log_context_event(
        "ingest_latency",
        {
            "source_type": "text",
            "image_count": 0,
            "total_ms": 10000,
            "stage_ms": {"text_extract_slices": 9000, "queue_stage": 15},
        },
    )
    telemetry.log_context_event(
        "store_image_latency",
        {
            "source_type": "store_image",
            "image_count": 3,
            "total_ms": 12000,
            "stage_ms": {"caption_image_1": 5000, "caption_image_2": 5000},
        },
    )

    summary = telemetry.summarize_context_events()

    assert summary["ingest_latency_events"] == 2
    assert summary["store_image_latency_events"] == 1
    assert summary["ingest_latency_median_ms"] == 26000
    assert summary["ingest_latency_p95_ms"] == 42000
    assert summary["store_image_latency_median_ms"] == 12000
    assert summary["recent_ingest_latency"][-1]["source_type"] == "text"
    assert summary["recent_store_image_latency"][-1]["image_count"] == 3
    assert summary["ingest_slow_stages"][0]["stage"] == "vision_extract"


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


def test_trace_eval_case_ids_survive_exclusions(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / "_wiki" / "meta").mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(vault))
    traces = [
        {"approved": True, "final_page": "rag", "title": "one"},
        {"approved": False, "final_page": "skipped", "title": "unapproved"},
        {"approved": True, "final_page": "agents", "title": "two"},
        {"approved": True, "final_page": "inference", "title": "three"},
    ]
    (vault / "_wiki" / "meta" / "traces.jsonl").write_text(
        "\n".join(json.dumps(t) for t in traces) + "\n", encoding="utf-8"
    )
    eval_harness.exclude_eval_case("trace-2", "noisy")

    cases = eval_harness._trace_eval_cases()

    assert [(c["id"], c["title"]) for c in cases] == [("trace-1", "one"), ("trace-3", "three")]


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
