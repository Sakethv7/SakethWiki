# LLM Transient Retry — Logic Flow

```text
Capture or background extraction
            |
            v
   llm_client.complete
            |
            v
   call selected provider  <----------------------+
            |                                     |
      +-----+------+                              |
      |            |                              |
   response      exception                        |
      |            |                              |
 validate       classify                          |
 contract          |                              |
      |       +----+-------------------+          |
      |       |                        |          |
      | INGEST_EXTRACT?            transient     |
      |       |                        |          |
      | one JSON repair       attempts remaining? |
      |       |                        |          |
      |  valid? return        terminal/backoff    |
      |                                |          |
      |                         +------+-----+     |
      |                         |            |     |
      |                        no           yes    |
      |                         |            |     |
      |                   terminal fail   bounded |
      |                                  backoff  |
      |                                     |     |
      |                                     +-----+
      v
 return one successful result
            |
            v
 caller parses extraction and performs queue/vault work once
```

## State transitions for background capture

```text
pending_extraction=true
        |
        +-- transient failures, retry budget remains --> no queue mutation
        |
        +-- successful final attempt -----------------> pending=false, extracted data stored
        |
        `-- retry budget exhausted -------------------> pending=false, extraction_error stored
                                                           |
                                                           `-- existing manual retry
```

## Attempt rules

1. Read and clamp retry configuration once for the logical completion.
2. Invoke the provider.
3. On success, return without another attempt.
4. On an exception, retry only if it is classified transient and attempts remain.
5. Compute a bounded exponential delay with jitter; a valid `Retry-After` may increase the delay only up to the configured cap.
6. After exhaustion, raise the last provider exception through the existing extraction error handling.
7. A non-empty invalid `INGEST_EXTRACT` response gets at most one compact JSON repair request; invalid output for all other tasks fails normally.
