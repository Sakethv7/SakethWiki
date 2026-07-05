# SakethWiki Next Session: Memory Roadmap

## Goal
Turn SakethWiki from a smart wiki with local retrieval into a stronger memory system that:
- resolves aliases consistently
- keeps embeddings opt-in
- consolidates overlapping pages automatically where safe
- stores user preference memory separately from page content
- resurfaces weak or stale concepts as part of an active review loop

## Current State
- Markdown is still the source of truth.
- `backend/memory_store.py` builds a local SQLite index at `_wiki/meta/memory.db`.
- `/chat` already uses that local memory index for retrieval.
- `EMBED_ENABLED=true` is required before any embeddings are computed.
- Weekly trace analysis already exists through `/analyze-traces` and the backend scheduler.

## What To Build Next

### 1. Identity Resolution
Unify aliases like `RAG`, `retrieval augmented generation`, `retrieval-augmented generation`, and related slug variants into one canonical concept.

Acceptance criteria:
- ✅ Alias lookup resolves to one canonical page slug through `backend/identity.py`.
- ✅ Retrieval returns the canonical page when an alias page and canonical page both exist.
- ✅ Lint and `/aliases` can flag alias duplication or redirect candidates.

Session progress:
- Added shared identity resolution from built-ins, `_wiki/meta/aliases.json`, and page frontmatter `aliases`.
- Wired canonical slugs into memory indexing/search, page reads, backlinks, graph links, approval writes, wikilink normalization, `/page`, `/add-link`, `/create-stub`, and `/consolidate`.
- Fixed fallback keyword retrieval so it scores every page, not just the last file in each concept folder.

Pushback:
- Do not build automatic alias-page deletion yet. Canonical resolution is safe; deletion/merge needs evidence from page content and source history.
- Do not fold tag hierarchy into this layer. Aliases are identity; tags are taxonomy. Mixing them will create subtle retrieval bugs.

### 2. Semantic Embeddings
Keep embeddings opt-in and use them only as a reranker over the local chunk index.

Acceptance criteria:
- Default behavior remains local lexical retrieval only.
- Embeddings run only when `EMBED_ENABLED=true`.
- Semantic scores never replace the local index; they only rerank or supplement it.

### 3. Background Consolidation
Detect duplicate pages and weakly overlapping pages automatically, then propose or apply merges where confidence is high.

Acceptance criteria:
- ✅ Vault scan surfaces near-duplicate pages through `/consolidation-candidates`.
- ✅ Clear duplicate pairs can be merged with one confirmation step through `/consolidate`.
- ✅ Weak overlaps are reported, not blindly merged.

Session progress:
- Added `backend/consolidation.py`.
- Candidate scan uses identity aliases first, then slug similarity plus concept-text overlap.
- Added `/consolidation-candidates`.
- Added a safety gate to `/consolidate`; non-high-confidence pairs require `force=true`.
- Fixed alias-source consolidation semantics: the source page remains the concrete page to delete, while the target resolves canonical.

Pushback:
- Consolidation is not contradiction resolution. Conflicting pages should usually enter active review first.
- Shared tags are not enough evidence to merge pages.

### 4. Long-Term Preference Memory
Store user preferences about wording, correction patterns, and question style separately from page content.

Examples:
- preferred tag names
- rejected page slugs
- repeated wording fixes
- style preferences for summaries and synthesis

Acceptance criteria:
- ✅ Preference memory is not stored inside the concept pages.
- ✅ Extraction and chat can use preference memory to shape future behavior.
- ✅ User corrections update this memory over time through approval/rejection traces.

Session progress:
- Added `backend/preference_memory.py`, persisted at `_wiki/meta/preferences.json`.
- Learned page corrections, tag corrections, rejected page slugs, and style preferences from trace events.
- Injected preference hints into extraction prompts and chat system prompts.
- Added `/preferences` for inspection.

Pushback:
- Do not store this in Markdown concept pages. That would mix behavioral memory with knowledge memory and pollute retrieval.
- Do not infer broad personality preferences from one-off events. Start with explicit correction counts.

### 5. Active Review Loop
Resurface weak, stale, or low-maturity concepts so the system compounds over time.

Signals:
- low `understanding_maturity`
- missing backlinks
- no recent reads
- conflicting definitions

Acceptance criteria:
- ✅ The system can generate a review queue from these signals.
- ✅ The queue prioritizes concepts that need attention.
- ✅ Review suggestions are visible via `/active-review` and `/review-queue`.

Session progress:
- Added `backend/active_review.py`.
- Scored review priority from maturity, backlinks, read/update recency, thin pages, entry count, and conflict markers.
- Added reason strings, raw signal payloads, and suggested actions for each review item.
- Upgraded `/review-queue` and added explicit `/active-review`.

Pushback:
- Do not treat age alone as review priority. Staleness matters only alongside maturity, graph position, and conflict/thinness signals.

## Suggested Build Order
1. Identity resolution.
2. Preference memory.
3. Active review loop.
4. Background consolidation.
5. Embeddings reranker, if still needed after the local retrieval pass is strong.

## Important Constraints
- Do not remove Markdown as the source of truth.
- Keep embeddings off by default.
- Keep automatic merges conservative.
- Do not mix preference memory into page content.
- Keep manual review available for ambiguous cases.

## Doubts To Verify In Next Session
These are places where the code or UI makes automation look more complete than it may actually be. Verify before assuming the process is fully automatic.

- `Weekly analysis`: the backend has a scheduler, but confirm it really runs continuously in the deployed app and is not only available as the `🧠 Learn` button in the UI.
- `Trace-to-insight loop`: confirm the scheduled run writes `system-insights.md` on its own, and that the next ingest actually reads the new `Prompt Hints` section without any manual refresh.
- `Health check automation`: confirm `/lint` only reports issues and that the `Apply` button is the real action trigger for `/add-link`, `/create-stub`, and `/consolidate`.
- `Orphan handling`: confirm orphaned pages are only flagged for manual review, not auto-fixed.
- `Auto-merge safety`: confirm `/consolidate` is only used when the match is genuinely safe and not just because two pages look similar.
- `Embedding mode`: confirm `EMBED_ENABLED` is the only switch that turns on embeddings, so the vault stays local by default.
- `Preference memory`: confirm this does not already exist under another name before building it from scratch.
- `Review queue`: confirm there is no hidden active-review scheduler beyond the existing maturity and health check paths.

## Files Likely To Change
- `backend/memory_store.py`
- `backend/main.py`
- `backend/vault_reader.py`
- `backend/wiki_writer.py`
- `frontend/src/App.jsx`
- `README.md`
- `ARCHITECTURE.md`
- `CONCEPTS.md`

## Definition of Done
The system should be able to:
- normalize concept aliases to one canonical memory entry ✅ first pass implemented
- optionally rerank retrieval with embeddings
- detect and propose safe merges on its own
- remember user preference patterns separately from content
- surface weak concepts for review without manual searching
