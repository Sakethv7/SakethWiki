import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import eval_harness
import main


def test_normalize_extraction_contract_backfills_curation_fields():
    data = main._normalize_extraction_contract(
        {
            "summary": ["Execution loop acts and observes tool feedback."],
            "diagram_plan": {"needed": False},
        },
        depth="medium",
    )

    assert data["source_verdict"] == "ingest"
    assert data["knowledge_shape"] == "none"
    assert data["educational_core"] == ["Execution loop acts and observes tool feedback."]
    assert data["diagram_plan"]["needed"] is False


def test_normalize_extraction_contract_reject_removes_diagram():
    data = main._normalize_extraction_contract(
        {
            "source_verdict": "reject",
            "discarded_context": ["Recipe steps are not durable wiki knowledge."],
            "summary": [],
            "diagram": "flowchart LR\n  A[Cookie] --> B[Bake]",
            "diagram_plan": {"needed": True, "type": "flowchart"},
        },
        depth="medium",
    )

    assert data["source_verdict"] == "reject"
    assert data["diagram"] == ""
    assert data["summary"] == ["Recipe steps are not durable wiki knowledge."]


def test_ingest_curation_eval_reports_missing_contracts(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    traces = vault / "_wiki" / "meta" / "traces.jsonl"
    traces.parent.mkdir(parents=True)
    rows = [
        {
            "approved": True,
            "title": "Loop taxonomy",
            "source_verdict": "source_only",
            "knowledge_shape": "taxonomy",
            "discarded_context": ["Conference chronology"],
            "diagram_plan": {"needed": True, "type": "loop-stack"},
        },
        {"approved": True, "title": "Old trace without curation"},
    ]
    traces.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    monkeypatch.setenv("VAULT_PATH", str(vault))

    report = eval_harness.run_ingest_curation_eval(limit=10)

    assert report["cases"] == 2
    assert report["contract_coverage"] == 0.5
    assert report["source_only"] == 1
    assert report["shape_counts"]["taxonomy"] == 1
    assert report["failures"][0]["suite"] == "ingest_curation"


def test_ingest_curation_judge_maps_llm_failures(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    traces = vault / "_wiki" / "meta" / "traces.jsonl"
    traces.parent.mkdir(parents=True)
    traces.write_text(
        json.dumps({
            "approved": True,
            "title": "Loop taxonomy",
            "final_page": "agent-loops",
            "source_verdict": "source_only",
            "knowledge_shape": "taxonomy",
            "summary": ["The post distinguishes execution, task, product, and system loops."],
            "key_concepts": ["execution loop", "task loop"],
            "educational_core": ["A loop needs a feedback signal and exit condition."],
            "discarded_context": ["Conference chronology"],
            "diagram_plan": {"needed": True, "type": "loop-stack"},
        }) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VAULT_PATH", str(vault))

    def fake_complete(**kwargs):
        assert kwargs["task"] == "ingest_curation_judge"
        return json.dumps({
            "cases": [{
                "case_id": "trace-1",
                "passed": False,
                "score": 0.4,
                "issues": ["educational_core is too generic"],
                "suggested_prompt_hint": "Prefer specific loop names over generic feedback claims.",
            }],
            "summary": "One curation case needs sharper core extraction.",
        })

    monkeypatch.setattr(eval_harness.llm_client, "complete", fake_complete)

    report = eval_harness.run_ingest_curation_judge(limit=5)

    assert report["ran"] is True
    assert report["pass_rate"] == 0.0
    assert report["failures"][0]["final_page"] == "agent-loops"
    assert "specific loop names" in report["failures"][0]["suggested_prompt_hint"]
