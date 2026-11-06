# API: Bulk Resolve Walkthrough

## Visual Level

```mermaid
flowchart TD
    A[ResolveWalkthrough]:::new --> B[ReviewPanel]:::changed
    A --> C[POST /approve/id]
    A --> D[POST /queue/id/compare]
    B --> E[Claim list sorted by risk]:::new
    B --> F[Action buttons]
    classDef new fill:#dbeafe,stroke:#2563eb;
    classDef changed fill:#fef3c7,stroke:#d97706;
```

Blue is a new component. Amber is an existing one that changes. The two routes are unchanged.

## HTTP routes (unchanged)

| Route | Body | Success | Errors |
|---|---|---|---|
| `POST /approve/{id}` | `{approved: bool, resolution?: "append"\|"replace"\|"keep_both"}` | `{success, action, file_written, resolution?, archived?}` | 404 not found. 409 `needs_review` (approve with no resolution). 409 `report_stale`. 500 `archive_failed` or vault write failure. |
| `POST /queue/{id}/compare` | none | `{conflict_report}` | 404 |

Invariant: a clip that overlaps, conflicts or repeats a page is never written without a `resolution`.

## Frontend contracts

`ResolveWalkthrough({ items, onClose, onFinished })`
- `items`: queue items with a `conflict_report`. Fixed at open.
- `onFinished(summary)`: `summary = { done: n, left: n, failed: n }`. Parent reloads the queue and calls `onApproved` when `done > 0`.

`ReviewPanel({ item, report, onResolve(action), busy, stale, onRefresh, error, extraActions })`
- Renders the summary line, the claims and the action buttons.
- `onResolve(action)` where action is `append | keep_both | replace | skip`.
- `extraActions` holds the "Decide later" button. The old modal passes none.

`sortClaims(claims)` returns a new array ordered `conflicts`, `changed`, `new`, `same`. Stable within a verdict.

`summaryLine(claims)` returns a string such as "3 new claims. 1 changes or conflicts with the page."

---

# Change 2: Weak targets and clearer actions

## Visual Level

```mermaid
flowchart TD
    A[capture_compare.CANDIDATE_FLOOR 0.50]:::changed --> B[pick_target returns no target when weak]:::changed
    B --> C[build_report band distinct]
    D[calibration_pairs.json plus 5 pairs]:::changed --> E[calibrate_compare gate-only run]
    F[actionLabels function]:::new --> G[ConflictReviewModal buttons]:::changed
    H[Queue bar Resolve all and Select all]:::changed --> I[ResolveWalkthrough]
    classDef changed fill:#dbeafe,stroke:#2563eb;
    classDef new fill:#dcfce7,stroke:#16a34a;
```

Blue is changed. Green is a new function. No route, schema or error shape changes.

## Backend

`capture_compare.CANDIDATE_FLOOR: float = 0.50` (was 0.35). Meaning: the lowest word-overlap score at which a page the extractor did not name becomes the compare target. Pages the extractor named still win at any score. Invariant: a report with `band` of `overlap`, `conflict` or `duplicate` always has a `target_page`.

## Calibration data

Each added pair uses the existing schema: `id`, `how_built`, `clip_title`, `clip_claims`, `suggested_page`, `label`, `hide_pages`. New ids: `p41` to `p45`. Label: `distinct`. Source: the five live clips, with the model-labeled claims left out because `distinct` has no claims.

## Frontend

`actionLabels(report, item)` returns `{ append, keep_both, replace, skip }` as display strings. It uses `report.target_page` and `item.suggested_page`.

`isStrongMatch(report)` is true when `report.match_score >= 0.60`. It controls the dark "suggested" button and the "weak match" note.

`QueueSection` bar: `selectAllChecked`, `onToggleAll()`. "Resolve all" opens `ResolveWalkthrough` with every queued clip where `needsReview` is true and extraction is ready.

# Change 3: A true merge action

## Visual Level

```mermaid
flowchart TD
    A[Modal] -->|POST merge_preview| B[Backend: model rewrites page]:::new
    B -->|merged_body, target_hash| A
    A -->|POST approve: resolution merge, merged_body, merge_target_hash| C[Backend: check hash]
    C -->|ok| D[Archive, write, dequeue]:::new
    C -->|stale| E[409 report_stale]:::failure
    classDef new fill:#dbeafe,stroke:#2563eb;
    classDef failure fill:#fee2e2,stroke:#dc2626;
```

Blue is new. Red is an error response.

## New route

`POST /queue/{id}/merge_preview`. No body. Reads the queue item and its `conflict_report.target_page`.

| Status | Body |
|---|---|
| 200 | `{ "merged_body": str, "old_body": str, "target_page": str, "target_hash": str, "notes": [str] }` |
| 404 | The item or the target page does not exist |
| 409 | The item has no target page (nothing to merge into) |
| 422 | `{ "error": "merge_rejected", "reason": str }` A check failed |
| 502 | `{ "error": "model_failed", "message": str }` |

The route writes nothing. The loopback guard applies as for all routes.

## Changed route

`POST /approve/{id}` accepts `resolution: "merge"`. When it does, `merged_body` and `merge_target_hash` are required. The existing rule stays: an approved clip with a target needs a chosen resolution.

Invariants:
- `merge_target_hash` must equal the current hash of the target page, or the route returns 409 `report_stale`.
- The old page is archived before the write.
- Frontmatter is never taken from model output.

## Frontend

`ConflictReviewModal` gains a fourth action, "Merge into {page}". It is shown for overlap and conflict bands. It opens a preview panel with Approve and Cancel. A 409 on approve reuses the existing stale banner.
