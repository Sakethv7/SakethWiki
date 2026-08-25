# Queue Bulk Review — Architecture

## Problem

Lekhni can enqueue dozens of already-extracted wiki candidates. The current SakethWiki queue hides `Save to wiki` and `Skip` inside each expanded card, forcing an open-review-close cycle for every candidate.

This change adds two faster paths without weakening the existing detailed review path:

1. Direct approve or reject from a collapsed card.
2. Select several cards and approve or reject the selection in one operation.

## Scope

This is a queue-review change only. It does not change extraction, curation prompts, page-writing rules, or the Markdown vault as the source of truth.

Pending or failed extractions cannot be approved in bulk. They may be rejected because rejection does not require extracted content.

## Components

### React queue UI

`QueueSection` continues to own queue loading and polling. It gains:

- a selection set keyed by queue item ID;
- a checkbox on each eligible card;
- always-visible `Approve` and `Reject` controls on collapsed, extracted cards;
- a selection toolbar with `Select all ready`, `Approve selected`, `Reject selected`, and `Clear`;
- a confirmation dialog before either bulk action;
- a result message that distinguishes completed and failed items.

The chevron/title region remains the expansion target. Buttons and checkboxes stop event propagation so actions do not accidentally open the card.

### FastAPI batch route

A new `POST /queue/batch-decision` endpoint accepts explicit item IDs and one decision. It reuses the same internal decision function as `POST /approve/{item_id}` so trace logging, preference learning, vault writes, and queue removal remain consistent.

### Queue and vault storage

The queue remains `hitl_queue.json`, protected by the existing file lock for mutations. Approved items still flow through `wiki_writer.py`; rejected items still produce rejection traces.

## Data flow

```text
Queue cards
   |-- single action ----------> existing single-decision API
   |
   `-- selected IDs
          -> confirmation
          -> batch-decision API
          -> process each ID through shared decision service
                 |-- approve -> wiki_writer -> Markdown vault + trace
                 `-- reject  -> remove queue item + trace
          -> per-item results
          -> remove successes from selection; retain failures for retry
          -> refresh queue
```

## Safety and failure behavior

- Bulk actions require explicit selection and confirmation.
- The confirmation names the decision and item count.
- Duplicate IDs are rejected by validation rather than processed twice.
- A practical request limit of 100 IDs prevents accidental oversized mutations.
- Batch approval is not atomic: one wiki write may succeed while another fails. The response exposes each outcome.
- Successfully processed cards disappear after refresh. Failed cards remain selected and visible.
- While a batch request is running, card and toolbar decision controls are disabled.
- An item removed by another tab returns `not_found`; it is not silently counted as success.

## Verification plan after approval

- Backend tests: validation, mixed success/failure, trace behavior, rejection, approval, and missing IDs.
- Frontend tests: collapsed-card buttons, propagation behavior, selection eligibility, confirmation, partial failure, and selection cleanup.
- Build the frontend.
- Run focused backend and frontend suites.
- Start the app through HTTP and smoke-test single approve/reject plus a mixed bulk selection against disposable test queue items.

