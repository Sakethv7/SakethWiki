# Capture Queue — Logic Flow

## Submit

```text
User clicks Add to queue
  -> client generates/retains capture_key for this click
  -> POST /queue-capture
       -> validate exactly one supported capture shape and payload bounds
       -> lock queue
       -> same capture_key exists?
            yes -> return existing item (no duplicate work)
            no  -> append queued item atomically
       -> signal worker
  -> UI clears this composer draft only after successful response
  -> user can enter the next capture immediately
```

If submission fails, the composer stays populated and displays the error. It must never clear optimistically.

## Worker

```text
Server starts
  -> lock queue; recover processing items to queued
  -> start one worker loop

Worker wakes
  -> lock queue; claim oldest queued item as processing
  -> no item? wait for signal/poll interval
  -> normalize source
       URL -> fetch URL
       text/Markdown -> use stored text
       images -> use stored images + optional user notes
  -> call existing extraction logic
       success -> lock queue; processing -> ready with draft
       failure -> lock queue; processing -> failed with sanitized message
  -> next item
```

## Retry and decision

```text
User clicks Retry on failed item
  -> POST /queue/{id}/retry
  -> lock queue; failed -> queued; clear error; increment attempt_count
  -> signal worker

User clicks Approve
  -> POST /approve/{id}
  -> reject unless status == ready
  -> existing approve behavior writes Markdown and trace
  -> existing queue removal occurs only after that result
```

## State transitions

```text
queued -> processing -> ready -> approved/rejected (removed)
                    `-> failed -> queued (explicit retry)

restart: processing -> queued
```

All other transitions are invalid and return a clear request error. A second retry request after the first one has already requeued the item is idempotently reported as already queued; it does not increment attempts again.
