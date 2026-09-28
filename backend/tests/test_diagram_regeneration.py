import asyncio
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


def _item(**overrides):
    item = {
        "id": "item-1", "revision": 0, "title": "Machine alert path",
        "summary": ["Sensor data reaches a gateway before an alert."],
        "key_concepts": ["sensor", "gateway", "alert"],
        "knowledge_shape": "architecture",
        "diagram_plan": {"needed": True, "type": "architecture", "reason": "Shows data movement."},
        "diagram": "flowchart LR\n  A[Sensor] --> B[Gateway]",
        "source_evidence": {"text": "Sensor data moves to a gateway and produces an alert.", "images": []},
        "diagram_revisions": [],
    }
    item.update(overrides)
    return item


def test_regeneration_plan_keeps_explicit_no_diagram():
    item = _item(diagram_plan={"needed": False, "type": "none"})

    plan = main._regeneration_plan(item, item["source_evidence"]["text"], "faithful_source")

    assert plan == {"needed": False, "type": "none", "reason": "Source has no approved visual structure."}


def test_candidate_validation_rejects_unsupported_labels():
    result = main._validate_diagram_candidate(
        "flowchart LR\n  A[Sensor] --> B[Gateway]\n  B --> C[Quantum Reactor]",
        {"needed": True, "type": "architecture"},
        "Sensor data moves to a gateway and produces an alert.",
        ["sensor", "gateway", "alert"],
    )

    assert result["selectable"] is False
    assert result["grounded_labels"] is False
    assert "Quantum Reactor" in result["reason"]


def test_candidate_validation_accepts_grounded_mermaid():
    result = main._validate_diagram_candidate(
        "flowchart LR\n  A[Sensor] --> B[Gateway]\n  B --> C[Alert]",
        {"needed": True, "type": "architecture"},
        "Sensor data moves to a gateway and produces an alert.",
        ["sensor", "gateway", "alert"],
    )

    assert result["selectable"] is True


def test_create_candidate_is_versioned_and_idempotent(monkeypatch):
    item = _item()
    stored = []

    monkeypatch.setattr(main.queue_manager, "get_by_id", lambda _id: item)

    def update_if_revision(_id, expected, updated):
        assert expected == 0
        stored.append(updated)
        return True, updated

    monkeypatch.setattr(main.queue_manager, "update_if_revision", update_if_revision)
    monkeypatch.setattr(
        main, "_generate_evidence_diagram",
        lambda *_args: "flowchart LR\n  A[Sensor] --> B[Gateway]\n  B --> C[Alert]",
    )
    monkeypatch.setattr(main.telemetry, "log_context_event", lambda *_args: None)

    request = main.DiagramRegenerationRequest(
        expected_revision=0, idempotency_key="request-1", intent="faithful_source"
    )
    result = asyncio.run(main.create_diagram_regeneration("item-1", request))

    assert result["revision"] == 1
    assert result["candidate"]["validation"]["selectable"] is True
    assert stored[0]["diagram"] == item["diagram"]  # generation does not replace the selected draft


def test_create_candidate_rejects_stale_revision(monkeypatch):
    monkeypatch.setattr(main.queue_manager, "get_by_id", lambda _id: _item(revision=2))
    request = main.DiagramRegenerationRequest(
        expected_revision=1, idempotency_key="request-1", intent="faithful_source"
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.create_diagram_regeneration("item-1", request))

    assert exc.value.status_code == 409


def test_select_candidate_changes_only_selected_diagram(monkeypatch):
    candidate = {
        "id": "candidate-1", "diagram": "flowchart LR\n  A[Sensor] --> B[Gateway]\n  B --> C[Alert]",
        "diagram_plan": {"needed": True, "type": "architecture", "reason": "Shows movement."},
        "validation": {"selectable": True},
    }
    item = _item(revision=1, diagram_revisions=[candidate])
    saved = []
    monkeypatch.setattr(main.queue_manager, "get_by_id", lambda _id: item)
    monkeypatch.setattr(
        main.queue_manager, "update_if_revision",
        lambda _id, expected, updated: (saved.append(updated) or True, updated),
    )

    result = asyncio.run(main.select_diagram_candidate(
        "item-1", main.DiagramSelectionRequest(expected_revision=1, candidate_id="candidate-1")
    ))

    assert result["revision"] == 2
    assert saved[0]["diagram"] == candidate["diagram"]
    assert saved[0]["selected_diagram_candidate_id"] == "candidate-1"
