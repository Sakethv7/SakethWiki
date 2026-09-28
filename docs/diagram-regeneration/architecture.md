# Evidence-Preserving Diagram Regeneration — Architecture

## Status

Proposed. This document authorizes no runtime implementation or queue mutation by itself.

## Problem

The current diagram-only regeneration path derives a new Mermaid diagram from the queue item's title, summary, and key concepts. It neither re-reads raw source material nor preserves the extraction-time diagram plan. When the model response is weak, it emits a generic fallback diagram. This can convert a source-grounded diagram into a plausible but unsupported one.

## Goal

Regenerate an optional diagram from the same source evidence and structural intent used by ingestion, validate it before it can be selected, and preserve a recoverable audit trail. Human review remains the only path that writes the candidate into the approved wiki note.

## Scope

- A diagram revision history stored on each pending queue item.
- Regeneration from immutable captured evidence: raw Markdown, fetched-source snapshot when available, and retained image payloads.
- A planning step that may choose `none` rather than fabricate a diagram.
- Deterministic Mermaid parse/render validation and source-label grounding checks.
- Optional reviewer intent and feedback for a new candidate.
- Optimistic concurrency for queue-item writes.

Out of scope: auto-approving a diagram, retroactively changing already-approved wiki pages, adding a hosted job queue, or generating a diagram when the evidence says none is useful.

## Components and data flow

```text
Review card
  -> POST /queue/{id}/diagram-regenerations
       (expected_revision, intent, feedback)
  -> queue lock + revision comparison
  -> immutable evidence bundle
       raw_markdown | retained source snapshot | images
  -> diagram planner
       plan: needed, type, reason
  -> source-grounded diagram generator
  -> validation
       Mermaid parse/render + plan/type + label grounding
  -> append candidate revision to queue item
  -> review card renders candidate or explicit no-diagram result
  -> existing explicit approval writes selected draft to vault
```

## Queue item contract

Existing extraction fields remain the current selected draft. The queue item gains:

```json
{
  "revision": 4,
  "diagram_revisions": [{
    "id": "uuid",
    "parent_revision": 3,
    "created_at": "ISO-8601",
    "intent": "faithful_source | explain_mechanism | compare_alternatives",
    "feedback": "optional bounded reviewer note",
    "evidence_hash": "sha256",
    "diagram_plan": {"needed": true, "type": "architecture", "reason": "..."},
    "diagram": "flowchart LR ...",
    "validation": {"renderable": true, "shape_match": true, "grounded_labels": true},
    "generator": {"model": "...", "prompt_version": "..."}
  }]
}
```

The initial extraction is represented as revision zero. Candidate generation never overwrites immutable evidence. Selecting a validated candidate changes the selected draft and increments `revision` under the queue lock.

## Invariants and failure modes

| Invariant | Failure prevented |
|---|---|
| Diagram-only regeneration uses immutable captured evidence, not summary alone. | A lossy summary creates invented structure. |
| `diagram_plan.needed = false` may end in an explicit no-diagram candidate. | Generic fallback creates a misleading visual. |
| A candidate must parse and render before selection. | Broken Mermaid reaches the review/approval path. |
| Every non-generic node label is grounded in source evidence or approved extracted concepts. | Hallucinated components and relationships. |
| Queue update requires matching `expected_revision`. | Two review tabs silently overwrite each other. |
| Existing approval remains explicit and unchanged. | Regeneration writes authoritative Markdown without human review. |

## Callers and downstream consumers

`QueueSection` initiates regeneration and displays version/conflict/validation state. `queue_manager.py` owns atomic compare-and-swap persistence. Diagram generation may reuse the ingestion planner and vision extraction, but cannot call `wiki_writer.py`. Existing queue approval, trace creation, `wiki_writer.py`, vault indexing, and browsing consume only the selected reviewed draft.

## Verification after approval

- Unit tests: planner chooses `none`, taxonomy, architecture, mechanism, and comparison from fixtures.
- Render tests: valid Mermaid is accepted; invalid syntax is rejected; no candidate is selected on validation failure.
- Grounding tests: source terms pass; unsupported node labels and relationships fail.
- Concurrency tests: matching revision succeeds, stale revision returns `409`, and a retry after a transport failure is idempotent with a request ID.
- Image fixture test: regeneration sees retained image evidence and preserves its actual labels/direction.
- Frontend build plus a bounded browser check: regenerate, inspect validation metadata, select a candidate, then explicitly approve.

## Rollback and recovery

Code rollback leaves existing queue items readable because new fields are optional. A faulty candidate is not selected automatically; reviewers can select its parent revision or discard it. No vault note is modified until the existing approval action. If the server fails mid-generation, the item retains its prior selected draft and no partial candidate is appended.
