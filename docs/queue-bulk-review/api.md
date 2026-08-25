# Queue Bulk Review — API Contract

## Existing single decision

### `POST /approve/{item_id}`

No external behavior change.

Request:

```json
{
  "approved": true,
  "redirect_note": null,
  "edits": null,
  "open_thread": false
}
```

The implementation will delegate to a shared internal decision function also used by the batch route.

## New batch decision

### `POST /queue/batch-decision`

Request:

```json
{
  "item_ids": ["queue-id-1", "queue-id-2"],
  "approved": true
}
```

Validation:

- `item_ids` must contain 1 to 100 non-empty, unique strings.
- `approved` is required.
- Bulk edits, redirect notes, and `open_thread` are out of scope. Candidates needing edits continue through detailed review.
- For approval, pending or extraction-failed items return an item-level `not_ready` failure.

Success response, including partial failure:

```json
{
  "success": false,
  "decision": "approved",
  "requested_count": 2,
  "completed_count": 1,
  "failed_count": 1,
  "results": [
    {
      "item_id": "queue-id-1",
      "success": true,
      "action": "approved",
      "file_written": "/absolute/vault/path/page.md"
    },
    {
      "item_id": "queue-id-2",
      "success": false,
      "code": "not_found",
      "message": "Item queue-id-2 was not found in the queue"
    }
  ]
}
```

`success` is true only when every item completed. HTTP 200 is used for a syntactically valid batch even when individual items fail, because the caller must inspect and display the per-item results.

Request-level errors:

- `422` for malformed input, duplicate IDs, or a count outside 1..100.
- `500` only when the batch handler itself cannot produce a result list. Expected item failures remain HTTP 200 results.

## Frontend contract

After a batch response:

- refresh `GET /queue`;
- clear selected IDs whose result has `success: true`;
- retain IDs whose result has `success: false` if they still exist;
- display completed and failed counts;
- expose each failed item message in the error/status region.

