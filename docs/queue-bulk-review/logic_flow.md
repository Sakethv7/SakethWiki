# Queue Bulk Review — Logic Flow

## Single-card decision

```text
User clicks Approve or Reject on collapsed card
  -> stop card-click propagation
  -> disable that card's decision controls
  -> POST /approve/{id}
       -> find current queue item
       -> if missing: return 404
       -> if approve and extraction is pending: preserve existing extraction behavior
       -> apply normal approve/reject side effects
  -> refresh queue
  -> show last-action status
  -> on failure: keep card and show an actionable error
```

## Selection

```text
Checkbox clicked
  -> stop card-click propagation
  -> toggle item ID in selectedIds

Select all ready clicked
  -> select items where pending_extraction is false
     and extraction_error is absent

Queue polling returns new items
  -> intersect selectedIds with IDs still present in queue
  -> never silently select newly arrived items
```

## Bulk decision

```text
User clicks Approve selected or Reject selected
  -> if selection empty: do nothing
  -> if approval contains ineligible item: block and explain
  -> show confirmation with decision + count
  -> user confirms
  -> disable all decision controls
  -> POST /queue/batch-decision { item_ids, approved }
       -> validate 1..100 unique IDs
       -> process IDs in request order through shared decision logic
       -> continue after an item failure
       -> return one result per ID plus counts
  -> refresh queue
  -> remove successful IDs from selection
  -> retain failed IDs in selection
  -> show `N completed, M failed`
  -> re-enable controls
```

## Eligibility rules

| Item state | Single approve | Single reject | Bulk approve | Bulk reject |
|---|---:|---:|---:|---:|
| Extracted and ready | Yes | Yes | Yes | Yes |
| Pending extraction | Existing behavior | Yes | No | Yes |
| Extraction failed | No | Yes | No | Yes |
| Missing/stale ID | No | No | Per-item failure | Per-item failure |

The distinction is deliberate: rejecting removes an unwanted queue record, while approval needs trustworthy extracted content.

## State invariants

- Selection contains only IDs from the latest loaded queue.
- A button click never toggles card expansion.
- Every successful decision has the same side effects whether invoked singly or in a batch.
- A failed item is never removed from the visible selection by the client.
- The summary count equals the number of per-item results.

