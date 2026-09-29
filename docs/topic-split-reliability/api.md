# Topic Split Reliability — Contract

## Internal planner contract

`SLICE_CONTENT` returns a JSON array of 1–5 objects:

```json
[{"title":"Short concept","paragraphs":[0,1],"concept_hint":"existing-page-slug-or-empty"}]
```

Indices must cover every input paragraph exactly once and preserve order when reconstructed. A malformed response gets one repair request with a 1,000-token minimum budget. A semantically invalid response is rejected locally.

## Telemetry

`topic_split` records `mode` (`llm`, `headings`, `paragraph_chunks`, or `none`), count when applicable, and a bounded reason for no split. It does not record source text or model output.

## Rollback

Set `LLM_SLICE_CONTRACT_REPAIR=false` to disable planner repair immediately. The deterministic validation and fallback remain active.
