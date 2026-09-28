# Evidence-Preserving Diagram Regeneration — Logic Flow

## Create a candidate

```text
Reviewer chooses intent and optional feedback
  -> client sends expected_revision + idempotency_key
  -> server reads queue item under lock
  -> item missing? 404
  -> expected_revision differs? 409 with current selected draft
  -> same idempotency_key already completed? return that candidate
  -> release lock while model work runs
  -> construct immutable evidence bundle
  -> plan diagram
       needed = false -> create validated no-diagram candidate
       needed = true  -> generate from evidence and plan
  -> validate candidate
       parse/render valid?
       plan type respected?
       labels grounded?
       no -> return validation failure; prior draft remains selected
  -> lock; revision still matches?
       no -> 409, do not append/select stale candidate
       yes -> append candidate, increment revision, retain current selection
```

## Select a candidate

```text
Reviewer selects a validated candidate
  -> POST /queue/{id}/diagram-selection
  -> lock; expected_revision matches?
       no -> 409 with newest item
       yes -> candidate exists and validated?
              no -> 422
              yes -> update selected diagram + diagram_plan; increment revision
  -> return selected draft for normal human approval
```

## Failure and recovery

```text
Source evidence unavailable
  -> return 422 with missing-evidence reason
  -> keep current selected diagram unchanged

LLM or render failure
  -> record sanitized attempt telemetry
  -> do not append a selectable candidate
  -> reviewer can retry with the same or new feedback

Network retry after candidate creation
  -> same idempotency_key returns original candidate

Stale tab submits/selects
  -> 409 includes current revision and selected diagram
  -> client refreshes; user decides whether to retry
```

## State transitions

```text
selected draft, revision N
  -> generate candidate (no selected-draft mutation)
  -> candidate valid
  -> explicit selection
  -> selected draft, revision N+1
  -> existing explicit queue approval
  -> Markdown vault write
```

The generation operation never writes the vault. A no-diagram candidate is selectable and writes `diagram=""` with `diagram_plan.needed=false` only after explicit selection.
