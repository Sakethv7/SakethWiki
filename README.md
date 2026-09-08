# SakethWiki

A personal knowledge system for capturing things learned in the wild — X/Twitter bookmarks, blogs, course snippets, screenshots — and building a compounding, evolving Obsidian vault.

**Vault location:** `~/SakethVault` (home dir — avoids macOS TCC restrictions for dock-launched apps)

---

## Stack

- **Backend:** FastAPI + Python, running on port 8001
- **Frontend:** React + Vite, Tailwind CSS (CDN), running on port 5173
- **LLMs:** Provider-routed by task (`anthropic` / `ollama` / `qwen` / OpenAI-compatible)
- **Storage:** Markdown source of truth + SQLite memory index (`_wiki/meta/memory.db`)
- **Retrieval:** Chunked lexical memory by default, optional embeddings when `OPENAI_API_KEY` is set

---

## Setup

### 1. Clone and install Python dependencies

```bash
git clone https://github.com/Sakethv7/SakethWiki.git
cd SakethWiki
cd backend && python3 -m venv venv && source venv/bin/activate
pip install -r ../requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env — set VAULT_PATH + one LLM provider key
# (default is Anthropic via LLM_PROVIDER=anthropic)
```

### LLM provider routing (optional)

SakethWiki now supports provider routing by task through env vars:

- `LLM_PROVIDER=anthropic|qwen|ollama|gemma|<custom>`
- `LLM_PROVIDER_<TASK>=...` (per-task override)
- `LLM_MODEL_<TASK>=...` (per-task model override)

Examples:

```bash
# Keep high-risk tasks on Anthropic
LLM_PROVIDER=anthropic

# Route chat to Qwen for lower cost
LLM_PROVIDER_CHAT_ANSWER=qwen
LLM_MODEL_CHAT_ANSWER=qwen-plus
```

Ollama local example:

```bash
ollama pull qwen2.5:7b
ollama pull qwen2.5vl:7b

LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434/v1
OLLAMA_MODEL_TEXT=qwen2.5:7b
OLLAMA_MODEL_VISION=qwen2.5vl:7b
```

Task keys currently used in code include:
`INGEST_EXTRACT`, `CHAT_ANSWER`, `INTERVIEW_VERIFY`, `INTERVIEW_GRADE`,
`EVOLUTION_CLASSIFY`, `TAG_CLASSIFY`, `ANALYZE_TRACES`, `LINT_SCAN`,
`LINT_JSON_FIX`, `CONSOLIDATE_PAGES`, `KNOWLEDGE_GAPS`.

Embedding-backed memory retrieval is optional and opt-in:

```bash
EMBED_ENABLED=true
OPENAI_API_KEY=...
EMBED_PROVIDER=openai
EMBED_MODEL=text-embedding-3-small
```

Without an embedding key, SakethWiki still builds the persistent SQLite memory
index and uses lexical chunk retrieval. With `EMBED_ENABLED=true`, the same
index stores vectors and blends semantic + lexical search at query time.

### Recommended hybrid profile (speed + judgment)

Use Gemini Flash for fast reversible UX paths, and Anthropic for source-of-truth
judgment paths:

```bash
LLM_PROVIDER=openai_compat
OPENAI_COMPAT_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
OPENAI_COMPAT_MODEL_TEXT=gemini-2.5-flash
OPENAI_COMPAT_MODEL_VISION=gemini-2.5-flash

# Critical integrity paths on Anthropic
LLM_PROVIDER_LINT_SCAN=anthropic
LLM_PROVIDER_LINT_JSON_FIX=anthropic
LLM_PROVIDER_CONSOLIDATE_PAGES=anthropic
LLM_PROVIDER_KNOWLEDGE_GAPS=anthropic
LLM_PROVIDER_EVOLUTION_CLASSIFY=anthropic
LLM_PROVIDER_ANALYZE_TRACES=anthropic

# Keep embeddings explicit. The Markdown vault and SQLite lexical index work
# without vectors; semantic embeddings are an opt-in derived index.
EMBED_ENABLED=false
```

Contract fallback guardrail (implemented in `llm_client`):
- `LLM_FALLBACK_TO_ANTHROPIC=true` to force fallback on contract failures.
- `LLM_FALLBACK_<TASK>=true|false` for per-task control.
- Critical tasks default to fallback even when global flag is unset.

### Config + Docs Sync Policy

This repo follows a strict sync policy on behavior changes:
- If `.env` variables are added/renamed/removed, update `.env.example` in the same change.
- If endpoint behavior, routing, or architecture changes, update `README.md`, `ARCHITECTURE.md`, and `CONCEPTS.md` in the same change.
- Prefer PRs that include code + docs together to avoid drift.

### 3. Create the vault structure

```bash
mkdir -p ~/SakethVault/_wiki/{concepts,sources,insights,meta}
```

### 4. Start the backend

```bash
cd backend
source venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8001
```

### 5. Start the frontend

```bash
cd frontend
npm install
npm run dev
```

Frontend at: http://localhost:5173

---

## macOS Dock App

An `.applescript` launcher is included. It starts the backend via uvicorn, serves the frontend build, and opens the app in a frameless browser window.

```bash
# Build frontend first
cd frontend && npm run build

# Then open SakethWiki.applescript in Script Editor and export as Application
```

> **Note:** The vault must live in `~/` (not `~/Documents/`) to avoid macOS TCC permission blocks when launching from the dock.

---

## iPhone Access via Tailscale

1. Install [Tailscale](https://tailscale.com) on your Mac and iPhone, log in with the same account
2. Find your Mac's Tailscale IP: `tailscale ip -4` (e.g. `100.x.y.z`)
3. Create `frontend/.env.local`:
   ```
   VITE_API_URL=http://100.x.y.z:8001
   ```
4. Rebuild: `cd frontend && npm run build`
5. Access on iPhone: `http://100.x.y.z:5173`

---

## API Reference

| Method | Path | Description |
|--------|------|-------------|
| POST | `/ingest` | Fetch URL or accept text/image, triage source value, extract metadata, stage to queue |
| POST | `/ingest-markdown` | Stage pasted markdown clip directly to curation-aware intelligence queue |
| POST | `/inbox/process` | Scan `_wiki/inbox/*.md` and stage clips to queue |
| GET | `/queue` | List all pending review items |
| GET | `/queue-status?ids=` | Resolve staged clip ids to pending/saved/rejected/unknown, for callers that staged items earlier and want their outcome |
| POST | `/approve/{id}` | Approve or reject a queued item |
| POST | `/chat` | Chat with your wiki through the persistent memory index |
| POST | `/chat-notes` | Attach a typed correction, contradiction, example, or nuance note to a chat answer trace |
| GET | `/pages?folder=` | List pages in a folder (concepts, sources, insights, meta) |
| GET | `/page/{name}` | Full content + parsed structured data for a page |
| DELETE | `/page/{name}` | Delete a page |
| POST | `/fix-page/{name}` | Normalise wikilinks and update entry count |
| **GET** | **`/lint`** | **Scan vault for structural issues (health check) and return report** |
| **GET** | **`/dashboard-stats`** | **Get 30-day learning health plus 112-day approved activity heatmap** |
| **POST** | **`/add-link`** | **Auto-insert wikilink from one page to another** |
| **POST** | **`/create-stub`** | **Create minimal stub page for missing concept** |
| **POST** | **`/calculate-maturity/{page}`** | **Calculate and update understanding maturity score for a page** |
| **POST** | **`/calculate-all-maturity`** | **Bulk calculate maturity scores for all concept pages** |
| POST | `/consolidate` | Merge two concept pages into one |
| POST | `/ingest-text` | Ingest plain text directly (no URL fetch) |
| POST | `/analyze-traces` | Run weekly self-learning analysis on approval traces |
| GET | `/system-insights` | Return current system insights and prompt hints |
| **POST** | **`/log-read`** | **Log a page read with duration to `meta/reads.jsonl`** |
| **GET** | **`/recent-reads`** | **Return last N unique recently-read pages** |
| **POST** | **`/edit-page/{name}`** | **Edit concept page body (preserves frontmatter, git commits)** |
| **POST** | **`/normalize-tags`** | **Map tag synonyms to canonical tags via tag-ontology.json** |
| **GET** | **`/tag-ontology`** | **Return the canonical tag ontology** |
| **GET** | **`/random-concept`** | **Return a random concept page name** |
| **POST** | **`/knowledge-gaps/{page_name}`** | **Generate 5 unanswered questions, prerequisites, and a concept diagram** |
| **GET** | **`/memory/status`** | **Inspect SQLite memory index state (pages, chunks, embeddings enabled)** |
| **POST** | **`/memory/reindex`** | **Rebuild or refresh the persistent memory index from markdown pages** |

### POST /ingest

```json
{
  "url": "https://example.com/article",
  "text": "optional extra context",
  "images": [{ "data": "<base64>", "mediaType": "image/png" }]
}
```

### POST /approve/{id}

```json
{
  "approved": true,
  "open_thread": false,
  "edits": { "title": "...", "summary": [], "tags": [], "suggested_page": "..." }
}
```

Set `open_thread: true` to add a `deep-dive` tag to the saved concept page — marks it for deeper research and surfaces it under the 🔍 Want more filter in Browse.

### POST /chat

```json
{
  "message": "What do I know about RAG?",
  "history": []
}
```

Asking "what do I know about X" returns a structured `knowledge_card` alongside the answer.

### POST /chat-notes

```json
{
  "note_type": "nuance",
  "note": "A weaker judge can work for first-pass triage, not final authority.",
  "question": "Can Llama 3.2 judge Llama 3.3?",
  "answer_excerpt": "Yes, but capability matters.",
  "pages_read": ["llm-as-judge"],
  "sources": ["_wiki/cs/llm-as-judge.md"]
}
```

Valid `note_type` values are `correction`, `contradiction`, `example`, and `nuance`. Chat notes append `event_type: chat_note` rows to `_wiki/meta/traces.jsonl` and `chat_note` rows to context telemetry. They are review evidence, not automatic concept-page edits.

### GET /lint

**Health Check — Scans entire vault for structural issues.** Returns report with:

```json
{
  "health_score": 61,
  "category_scores": {...},
  "inconsistencies": [{"pages": ["page1", "page2"], "issue": "..."}],
  "missing_connections": [{"from_page": "X", "to_page": "Y", "reason": "..."}],
  "suggested_articles": [{"title": "...", "reason": "..."}],
  "orphaned_pages": ["page_name", ...]
}
```

**Frontend:** Click "Health" button in Browse tab to run, then use checkboxes to auto-apply fixes via `/add-link`, `/create-stub`, `/consolidate`.

### POST /add-link

**Auto-insert wikilink from one page to another.**

```json
{
  "from_page": "inference",
  "to_page": "cpu-vs-gpu-for-ml"
}
```

Response: `{"added": true, "message": "Added [[cpu-vs-gpu-for-ml]] to inference"}`

Appends to existing "See also:" line or creates new section. Idempotent — won't duplicate existing links.

### POST /create-stub

**Create minimal stub page so it can be filled in later via Capture.**

```json
{
  "slug": "auto-differentiation",
  "reason": "Referenced in gradient-descent but no dedicated page"
}
```

Response: `{"created": true, "slug": "auto-differentiation", "message": "Created stub page 'Auto Differentiation'"}`

Creates file with frontmatter (`tags: []`, `entry_count: 0`, `understanding_version: 1`) and placeholder text pointing to Capture for content.

### POST /calculate-maturity/{page}

**Calculate and persist understanding maturity score for a concept page.**

Scoring formula (0–100):
- Source count: 30% (more sources = higher confidence)
- Recency: 20% (fresh updates = higher)
- Incoming links: 25% (more references = more important)
- Evolution count: 15% (multiple updates = mature)
- Contradiction markers: 10% (conflicting sources = lower)

Response:
```json
{
  "page": "attention-mechanisms",
  "understanding_maturity": 72,
  "components": {
    "source_count": 4,
    "recency_score": 95,
    "backlink_count": 6,
    "understanding_version": 3,
    "contradiction_count": 0
  }
}
```

Updates the page frontmatter with `understanding_maturity: 72` and displays as a progress meter on the concept page.

### POST /calculate-all-maturity

**Bulk calculate maturity scores for all concept pages.**

No request body required. Iterates through all pages and calls `/calculate-maturity` for each.

Response:
```json
{
  "total_pages": 18,
  "processed": 18,
  "failed": 0,
  "message": "Updated maturity scores for 18 pages"
}
```

### POST /lint (with caching)

**Health Check — Scans entire vault for structural issues.** 

Supports intelligent caching to save time and costs:

```json
{
  "save": false,
  "force_refresh": false
}
```

- `save`: If true, writes the lint report to `_wiki/insights/`
- `force_refresh`: If true, bypasses cache and runs full lint scan (useful after adding pages)

**Cache behavior:**
- Cache is stored in `_wiki/meta/lint-cache.json` with metadata (timestamp, page list hash)
- Cache is valid if: <24 hours old AND page list hasn't changed
- When cache is used: response includes `"from_cache": true` (takes ~8ms instead of 30-40s)
- When cache is invalid or force_refresh=true: response includes `"from_cache": false` (runs full scan)

**Cost impact:**
- First run: ~$0.05-0.10 + 30-40s latency
- Cached runs: $0 + ~8ms latency
- **Result:** Save ~$0.10/day on repeated health checks

### GET /dashboard-stats

**Learning Metrics — Returns learning statistics for the last 30 days plus a 112-day approved-activity heatmap.**

No request body needed. Displays in the **Dashboard** tab:

```json
{
  "period_days": 30,
  "heatmap_days": 112,
  "total_events": 16,
  "total_approved": 9,
  "total_rejected": 7,
  "approval_rate": 0.5625,
  "unique_concepts": 7,
  "activity_by_date": {
    "2026-04-15": 1,
    "2026-04-18": 8
  },
  "learning_velocity": {
    "entries_per_week": 2.1,
    "concepts_per_week": 1.63
  },
  "top_tags": [
    { "tag": "Engineering", "count": 6 },
    { "tag": "LLM", "count": 5 }
  ],
  "top_sources": [
    { "source": "text", "count": 4 }
  ],
  "new_concepts_this_week": 6,
  "concepts_touched_this_week": 7
}
```

**Metrics included:**
- **Approved activity heatmap:** 16-week grid of approved ingestions only
- **Learning velocity:** Approved entries per week and unique approved concepts per week
- **Approval quality:** Approved, rejected/skipped, and approval rate for the last 30 days
- **Approval rate definition:** `approved / (approved + rejected)` ingest decisions in the dashboard period; this measures curation selectivity, not model correctness
- **Top tags:** 10 most-referenced tags with frequency counts
- **Top sources:** Source type breakdown (tweets, articles, etc.)
- **Weekly badges:** True new concepts this week and concepts touched this week
- **Summary metrics:** 30-day approved/rejected aggregates and unique concepts

**Frontend visualization:**
- Dashboard tab with learning-health cards, compact Operations health, recent reads, review-due items, activity heatmap, tag breakdown, and source list
- Learning data is derived from `_wiki/meta/traces.jsonl`; Operations health is derived from runtime telemetry logs and displays the log date range. Historical usage rows without provider token data are estimated from character counts and labeled as estimated.

---

## Vault Structure

```
~/SakethVault/
└── _wiki/
    ├── cs/            ← Concept pages: CS / ML / DSA / systems — evolve over time
    ├── science/       ← Concept pages: math and science
    ├── sources/       ← One .md per URL ingested (immutable record)
    ├── insights/      ← Synthesised insight pages
    ├── open-threads/  ← Concepts flagged for deeper research (deep-dive)
    ├── lectures/      ← Lecture / talk capture notes
    ├── assets/        ← Pasted images referenced by pages
    ├── meta/          ← System state (memory.db, traces.jsonl, telemetry, index cache)
    └── index.md       ← Auto-rebuilt on every write
```

> A `humanities/` folder is recognised by the reader but is not a Browse tab
> until it holds pages. Concept retrieval and the knowledge graph currently
> span `cs/` and `science/`.

---

## Self-Learning System

Every approve/reject event writes a trace to `_wiki/meta/traces.jsonl`. Chat-note events also write typed trace evidence when you add corrections, contradictions, examples, or nuance under a chat answer. Once a week (auto) or on demand via the 🧠 Learn button in Browse, the routed analysis model (default Anthropic Sonnet) analyzes traces and writes structured findings to `_wiki/meta/system-insights.md`:

- Which page suggestions were wrong most often
- Tag confusion patterns (e.g. `Agentic` vs `Agents`)
- Sources of duplicates and rejection patterns
- **Prompt hints** — auto-injected into the next extraction prompt so corrections propagate automatically
- Routing and architecture recommendations surfaced to you

The ingestion loop is approve → trace → weekly analysis → insights → extraction prompt → better next extraction. The chat-note loop is answer → typed note → trace/telemetry → review evidence for better concept pages and future answers.

### GET /random-concept

Returns a randomly chosen concept page.

```json
{ "name": "kv-cache" }
```

Frontend: 🎲 **Random** button in Browse header opens the page directly.

### POST /knowledge-gaps/{page_name}

Generates unanswered questions from a concept page plus prerequisites and a diagram.

Response:
```json
{
  "gaps": [
    { "q": "How does KV cache layout affect GPU memory bandwidth?", "why": "The page mentions cache cost but not memory layout tradeoffs." },
    { "q": "When does speculative decoding reduce end-to-end latency?", "why": "Related optimization is referenced but not explained." }
  ],
  "prerequisites": ["attention-mechanisms", "transformers"],
  "diagram": "graph TD\n  A[Decode token] --> B[Check KV-cache]..."
}
```

### POST /log-read

```json
{ "page": "kv-cache", "duration_seconds": 42 }
```

Appends to `_wiki/meta/reads.jsonl`. Used by the frontend to log read duration when navigating away.

### GET /recent-reads

Returns last N unique recently-read pages (default N=10).

### POST /normalize-tags

```json
{ "tags": ["Agentic", "LLM", "MLops"] }
```

Response:
```json
{ "normalized": ["Agents", "LLM", "MLOps"], "mappings": {"Agentic": "Agents", "MLops": "MLOps"} }
```

### POST /edit-page/{name}

```json
{ "content": "Updated body markdown here..." }
```

Preserves existing frontmatter, writes body, best-effort git commit. Used by the inline Edit modal.

---

## iOS Shortcut Integration

The `/ingest` endpoint detects iOS clients (`CFNetwork`/`Darwin`/`Shortcuts` in User-Agent) and returns immediately (~20ms) to avoid Shortcuts' HTTP timeout. Extraction runs as a background `asyncio` task and patches the queue item in-place when done.

**Frontend:** Pending items show an "Extracting…" spinner badge. The queue auto-polls every 3 seconds until the status clears.

**To use:** Create a Shortcuts action with "Get Contents of URL" → POST to `http://<tailscale-ip>:8001/ingest` with the share sheet URL.

---

## Image Capture

Paste images anywhere on the page (Cmd+V) — no textarea focus required. Or drag-and-drop onto the Capture card (orange highlight on hover). Images are base64-encoded and sent with `/ingest`. If text is present with images, the frontend still sends the images to the vision extraction path so visual structure can become Mermaid diagrams; the images are also saved as vault assets. Tap a thumbnail to view full-size; tap `+` to add more.

Capture responses include latency metadata. `/ingest` logs stage timings for fetch, slicing, image uncertainty extraction, web gap search, vision/text extraction, and queue staging. `/store-image` logs image decode, caption, and write timings. The preview card shows client/server timing for the current run, and Operations → Telemetry keeps recent ingest/image-save latency plus slow-stage summaries.

Image asset captioning is optional metadata. Large pasted images skip the separate caption LLM call and fall back to deterministic filenames so full-resolution screenshots do not create expensive or noisy `IMAGE_CAPTION` contract failures. The main `/ingest` vision path still receives the images for knowledge extraction and diagram recovery.

Operations → Usage summarizes LLM token and cost telemetry by task, route, and recent expensive call. Provider-reported token usage is used when available; otherwise SakethWiki estimates tokens from character counts. Costs use built-in per-million-token defaults for common configured models and can be overridden with environment variables such as `LLM_PRICE_ANTHROPIC_CLAUDE_SONNET_4_6_INPUT_PER_1M` and `LLM_PRICE_ANTHROPIC_CLAUDE_SONNET_4_6_OUTPUT_PER_1M`. The UI displays cost per 1M tokens instead of raw dollars per token because raw per-token values are too small to read safely.

Operations → Queue renders staged actions as approval cards: proposed change, reason, evidence, eval gate explanation, and the concrete mutation behind Approve. Raw JSON remains available behind a disclosure for debugging, but approval should not require reading raw machine payloads.

Operations header commands are separated by side effect:
- **Run safety evals:** replay checks and write an eval report; no settings changes.
- **Write inference report:** summarize LLM/context telemetry, tokens, cost, and failures; no settings changes.
- **Find improvement candidates:** run the trace critic and stage action cards; does not apply them.
- **Run system loop:** route telemetry into bounded actions; may auto-apply low-risk fixes and stage medium/high-risk changes.

After a command finishes, Operations shows a Last operation banner with what ran, what changed, and what to inspect next. Read-only report commands route to Evals or Reports. Trace critic and system-loop runs route to Queue only when there are action candidates to review.

Operations → Evals separates two layers:

- **System-level eval:** checks trace schema integrity, chat/context telemetry visibility, dropped retrieved chunks, low source coverage, LLM task errors, pending action candidates, and the recent system-action trace stream.
- **Wiki/content eval:** replays preference memory, retrieval cases, and ingestion curation quality.

Runtime edits should be justified by system-level evidence first. Wiki cleanup issues belong in Browse/Health or curation review, not in the same score as routing, context-budget, or telemetry failures.

For high-stakes planning, use a brain/executor split: an expensive planning model proposes the plan, invariants, and acceptance checks; Codex executes the patch, runs tests, and commits. The handoff artifact should be plain Markdown with goal, constraints, files likely involved, acceptance checks, and explicit non-goals.

---

## Tag Normalization Script

To normalize tags across existing vault pages (one-off cleanup):

```bash
cd backend && source venv/bin/activate

# Dry run first — see what would change
python normalize_vault_tags.py --dry-run

# Apply
python normalize_vault_tags.py
```

Reads synonyms from `_wiki/meta/tag-ontology.json` and rewrites frontmatter tags in all concept pages.

---

## Running Tests

```bash
cd backend
source venv/bin/activate
pytest tests/
```

---

## Focus Mode (Packaging)

SakethWiki now ships with a packaging-first UX mode in the frontend.

- **Primary loop:** `Capture` + `Ask`
- **Secondary surfaces:** `Library` (Browse) + `Insights` (Dashboard)
- **Onboarding:** one-time first-run card with quick actions
- **Toggle:** header button switches between `Focus Mode` and `Classic Tabs`

This keeps compounding wiki capabilities intact while reducing daily interaction friction.

## App Icon

- Web app icon now uses: `frontend/public/factorymind-mark.svg`
- macOS app icon now uses: `resources/AppIcon.icns`

If you re-export the AppleScript app, assign `resources/AppIcon.icns` as the application icon in Script Editor / Finder info panel.

## Web Clipper Integration (New Default Flow)

Capture is now split into transport vs intelligence:

- **Transport:** Obsidian Web Clipper writes markdown into vault (`_wiki/inbox`) or you paste markdown in the Capture box.
- **Intelligence:** SakethWiki parses clip content, separates durable educational signal from discarded event/social context, proposes `suggested_page` / tags / links / diagram plan, and stages it to HITL queue.
- **Decision:** You approve merge/create/discard in the same queue UI.
- **Eval:** Manual eval runs include a bounded curation judge that samples recent traces and flags weak kept cores, missing discarded context, and unjustified diagram plans.

### Option A — paste markdown directly

Paste markdown into Capture and click Process. The frontend auto-routes markdown-like input to `/ingest-markdown`.

### Option B — clip directly into vault

Save clip files as `.md` under:

```bash
~/SakethVault/_wiki/inbox/
```

Then click **Process inbox clips** in Capture (or call `/inbox/process`).

Processed files are moved to:

```bash
~/SakethVault/_wiki/inbox/processed/
```

## Lekhni Integration

[Lekhni](https://github.com/Sakethv7/lekhni) is a separate local app that
turns recordings, transcripts, and pasted notes into generated Markdown
notes. It's the upstream source for a third capture path, alongside the Web
Clipper and inbox flows above: a Lekhni session note with a technical note
type (system design, talk, AI engineering, paper/research) can carry a
`## Reusable Patterns` section, and each pattern in it becomes its own
SakethWiki clip.

- **Transport:** Lekhni's `bridge_sakethwiki.py` posts each pattern to
  `POST /ingest-markdown` with `source_url` set to `lekhni://<session_id>`,
  so every clip traces back to the Lekhni session it came from.
- **Dedup:** if a clip with the same content was already staged in a prior
  push, `/ingest-markdown` returns `409` and Lekhni reconciles it against
  `_wiki/meta/processed_clips.jsonl` instead of re-queuing a duplicate.
- **Intelligence + Decision:** identical to the Web Clipper flow above —
  clips land in the same HITL queue and go through the same
  approve/reject/merge decision.
- **Status round-trip:** Lekhni polls `GET /queue-status?ids=...` to find
  out whether previously staged items were approved or rejected, since
  approve/reject removes them from the live `/queue` and their outcome only
  survives in `traces.jsonl`. This is how a Lekhni session shows a
  `sent` / `partial` / `saved` / `rejected` badge without SakethWiki having
  to push anything back to Lekhni.

In short: Lekhni decides what's worth reviewing and stages it; SakethWiki
decides what's worth keeping and where it lives long-term.
