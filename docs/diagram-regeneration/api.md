# Evidence-Preserving Diagram Regeneration — API Contract

## `POST /queue/{id}/diagram-regenerations`

Creates a validated diagram candidate without changing the selected diagram.

Request:

```json
{
  "expected_revision": 4,
  "idempotency_key": "6aed4224-f63c-47d1-8aad-fd36c4eab5a3",
  "intent": "faithful_source",
  "feedback": "Keep the feedback loop and original arrow direction."
}
```

`intent` is one of `faithful_source`, `explain_mechanism`, or `compare_alternatives`. `feedback` is optional and bounded; it is instruction context, not authoritative source evidence.

Success (`201`):

```json
{
  "id": "queue-item-id",
  "revision": 5,
  "candidate": {
    "id": "candidate-id",
    "diagram_plan": {"needed": true, "type": "architecture", "reason": "Shows data movement between components."},
    "diagram": "flowchart LR\n  A[Sensor] --> B[Gateway]",
    "validation": {"renderable": true, "shape_match": true, "grounded_labels": true}
  },
  "selected_candidate_id": "previous-candidate-id"
}
```

Errors: `404` queue item absent; `409` stale `expected_revision`; `422` immutable source evidence absent or candidate failed validation; `429`/`503` provider availability failure. A rejected candidate never changes the selected draft.

## `POST /queue/{id}/diagram-selection`

Selects an already validated candidate for the normal queue review/approval path.

Request:

```json
{
  "expected_revision": 5,
  "candidate_id": "candidate-id"
}
```

Success (`200`) returns the updated selected draft and revision. Errors: `404` missing item/candidate, `409` stale revision, `422` candidate is not validated.

## Compatibility and deprecation

`POST /queue/regenerate/{id}` stays available for `mode="full"`. Its `mode="diagram"` option returns `410 Gone`; the frontend uses the new candidate endpoint for diagram work. This prevents any caller from retaining the unsafe summary-only behavior.

## Telemetry

Each attempt records queue item ID, evidence hash, intent, plan type, model/prompt version, validation outcomes, latency, failure class, and candidate ID. It must not record raw image bytes, raw Markdown, or full reviewer feedback in telemetry.
