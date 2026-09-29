# Capture Queue — API Contract

## `POST /queue-capture`

Persists a capture and returns before fetch or extraction begins.

Request:

```json
{
  "capture_key": "2a470eb3-b72e-472c-840e-a6da08f2b2c7",
  "url": "https://example.com/article",
  "text": null,
  "images": null,
  "user_notes": null,
  "source_type": "url"
}
```

Exactly one primary source is required: `url`, non-empty `text`, or a non-empty `images` list. `user_notes` is permitted only with images. URL + explanatory text is deliberately not supported in v1; this avoids ambiguous extraction provenance. The endpoint enforces maximum text length, image count, per-image decoded size, and total decoded payload size before persistence; exact limits will be selected from current request-size evidence during implementation and exposed in validation errors.

Success response (`201` for new, `200` for an idempotent replay):

```json
{
  "id": "queue-item-id",
  "capture_key": "2a470eb3-b72e-472c-840e-a6da08f2b2c7",
  "status": "queued",
  "deduplicated": false,
  "queued_at": "2026-09-10T12:00:00"
}
```

Errors: `400` for invalid source shape, `413` for payload bounds, and `422` for invalid request fields. A request never creates a partial queue item.

## `GET /queue`

The existing endpoint remains. Items additionally expose `status`, `attempt_count`, and state timestamps. `pending_extraction` remains temporarily derived for legacy UI compatibility as `status in {queued, processing}`.

## `POST /queue/{id}/retry`

Only a `failed` item can be retried. It returns the requeued item. It does not execute extraction inline.

Errors: `404` missing item; `409` item is not failed (including already queued/processing/ready).

## Approval compatibility

`POST /approve/{id}` and `POST /queue/batch-decision` accept only `ready` items for approval. Reject remains valid for `queued`, `processing`, `ready`, and `failed` because it removes a capture and performs no vault write. Existing response shapes stay compatible.

## Client contract

The composer obtains a UUID once per Add-to-queue click and reuses it only if the same network request is retried. It clears text, images, and optional notes only after a successful queue response. Queue polling renders truthful state labels and only exposes Retry for failed items; it does not infer completion from elapsed time.
