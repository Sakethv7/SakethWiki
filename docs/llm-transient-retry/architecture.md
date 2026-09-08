# LLM Transient Retry — Architecture

## Problem

SakethWiki currently makes one provider request per LLM completion. A transient provider overload such as Anthropic HTTP 529 immediately escapes through `_extract_with_sonnet`, becomes `LLM extraction failed`, and leaves the capture in a failed state even though the same request would often succeed seconds later.

## Scope

Add bounded retries inside `backend/llm_client.py`, immediately around each provider request. This covers all callers of `llm_client.complete`, including foreground capture and background URL extraction, without retrying queue writes, vault writes, approval traces, or other persistent mutations.

The change does not add an infinite worker, silently switch models, retry invalid JSON, retry authentication/configuration errors, or automatically approve captured material.

## Components

### Retry policy

The shared client classifies failures before retrying:

- Retry HTTP 408, 409, 429, 500, 502, 503, 504, and 529.
- Retry connection and timeout failures.
- Do not retry 4xx authentication, permission, validation, or missing-resource failures.
- Use three total attempts by default (two retries).
- Use exponential delay with bounded jitter; honor a valid provider `Retry-After` value when present.
- Cap each delay so one completion cannot wait indefinitely.

Environment settings provide an operational escape hatch while defaults remain safe:

- `LLM_MAX_ATTEMPTS` (default `3`)
- `LLM_RETRY_BASE_SECONDS` (default `1`)
- `LLM_RETRY_MAX_SECONDS` (default `8`)

### Provider calls

Both `_anthropic_complete` and `_openai_compat_complete` remain single-attempt transport functions. `complete` invokes a small retry executor around the selected primary provider and, where already supported, around the Anthropic fallback provider. Keeping retry orchestration above transports gives both provider families the same policy and makes sleeping injectable in tests.

### Telemetry

The final LLM call telemetry record gains attempt counts for the primary and fallback legs. Failed intermediate attempts are logged through Python logging without recording prompt content or secrets. The terminal error still reaches the existing UI path after the retry budget is exhausted.

## Invariants

- No queue or vault mutation occurs between LLM retry attempts.
- A successful provider response is returned once and processed once by the caller.
- Permanent client errors fail immediately.
- Retry count and delay are bounded even with invalid environment values or provider headers.
- Existing provider routing and contract validation behavior remains unchanged.
- The user's failed queue item remains recoverable through the existing manual retry control if all attempts fail.

## Failure modes

- A provider may finish a request while the client loses the response. A retry can therefore incur duplicate model cost, but it cannot duplicate local queue/vault writes because those occur after `complete` returns.
- Sustained overload still fails after the bounded retry budget; the UI must continue to show the retry action.
- Excessive retry delays can make foreground capture feel stalled, so delays are capped and attempts remain configurable.
- Retrying invalid model output could multiply cost without fixing transport health; contract failures are deliberately excluded.

## Verification plan

- Unit-test 529 overload followed by success.
- Unit-test exhaustion after exactly three attempts.
- Unit-test permanent 401 failure with one attempt.
- Unit-test connection/timeout retry.
- Unit-test capped delay and `Retry-After` handling.
- Verify telemetry attempt counts on success and failure.
- Run focused backend tests, then the full backend suite.

