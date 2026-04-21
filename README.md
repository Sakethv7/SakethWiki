# SakethWiki

A personal knowledge system for capturing things learned in the wild — X/Twitter bookmarks, blogs, course snippets, screenshots — and building a compounding, evolving Obsidian vault.

**Vault location:** `~/SakethVault` (home dir — avoids macOS TCC restrictions for dock-launched apps)

---

## Stack

- **Backend:** FastAPI + Python, running on port 8001
- **Frontend:** React + Vite, Tailwind CSS (CDN), running on port 5173
- **LLMs:** `claude-sonnet-4-6` (extraction, summaries, lint) · `claude-haiku-4-5` (evolution analysis, chat, formatting)
- **Storage:** Flat Markdown files — no database, no embeddings

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
# Edit .env — set ANTHROPIC_API_KEY and VAULT_PATH
```

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
| POST | `/ingest` | Fetch URL or accept text/image, extract metadata, stage to queue |
| GET | `/queue` | List all pending review items |
| POST | `/approve/{id}` | Approve or reject a queued item |
| POST | `/chat` | Chat with your wiki (keyword-matched context + Claude) |
| GET | `/pages?folder=` | List pages in a folder (concepts, sources, insights, meta) |
| GET | `/page/{name}` | Full content + parsed structured data for a page |
| DELETE | `/page/{name}` | Delete a page |
| POST | `/fix-page/{name}` | Normalise wikilinks and update entry count |
| **GET** | **`/lint`** | **Scan vault for structural issues (health check) and return report** |
| **GET** | **`/dashboard-stats`** | **Get learning metrics for the last 30 days (activity, velocity, tags, sources)** |
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
| **POST** | **`/generate-summary/{name}`** | **Generate study summary: one-liner, paragraph, prerequisites, self-test Q&A, diagram** |

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
- `force_refresh`: If true, bypasses cache and runs full Sonnet scan (useful after adding pages)

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

**Learning Metrics — Returns learning statistics for the last 30 days.** 

No request body needed. Displays in the **Dashboard** tab:

```json
{
  "period_days": 30,
  "total_approved": 9,
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
    { "source": "text", "count": 9 }
  ],
  "new_concepts_this_week": 7
}
```

**Metrics included:**
- **Activity timeline:** Bar chart of entries added by date (last 14 days)
- **Learning velocity:** Entries per week and unique concepts per week
- **Top tags:** 10 most-referenced tags with frequency counts
- **Top sources:** Source type breakdown (tweets, articles, etc.)
- **Weekly badge:** Count of new concepts added this week
- **Summary metrics:** 30-day aggregates (total approved, unique concepts)

**Frontend visualization:**
- Dashboard tab with activity timeline chart, velocity cards, tag breakdown, and source list
- All data derived from `_wiki/meta/traces.jsonl` (no LLM cost)

---

## Vault Structure

```
~/SakethVault/
└── _wiki/
    ├── concepts/     ← One .md per concept — evolves over time
    ├── sources/      ← One .md per URL ingested (immutable record)
    ├── insights/     ← Synthesised insight pages
    ├── meta/         ← System pages (index, log)
    └── index.md      ← Auto-rebuilt on every write
```

---

## Self-Learning System

Every approve/reject event writes a trace to `_wiki/meta/traces.jsonl`. Once a week (auto) or on demand via the 🧠 Learn button in Browse, Claude Sonnet analyzes the traces and writes structured findings to `_wiki/meta/system-insights.md`:

- Which page suggestions were wrong most often
- Tag confusion patterns (e.g. `Agentic` vs `Agents`)
- Sources of duplicates and rejection patterns
- **Prompt hints** — auto-injected into the next extraction prompt so corrections propagate automatically
- Routing and architecture recommendations surfaced to you

The loop: approve → trace → weekly analysis → insights → extraction prompt → better next extraction.

### GET /random-concept

Returns a randomly chosen concept page.

```json
{ "name": "kv-cache" }
```

Frontend: 🎲 **Random** button in Browse header opens the page directly.

### POST /generate-summary/{name}

Generates a structured study summary for a concept page using `claude-sonnet-4-6`.

Response:
```json
{
  "one_liner": "KV-cache stores attention keys/values so decoding skips recomputation.",
  "paragraph": "During autoregressive inference...",
  "prerequisites": ["attention-mechanisms", "transformers"],
  "self_test": [
    { "q": "What does KV-cache store?", "a": "Keys and values from attention layers..." },
    ...
  ],
  "diagram": "graph TD\n  A[Decode token] --> B[Check KV-cache]..."
}
```

Frontend: **Study** button on concept page header opens a modal with collapsible Q&A.

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

Paste images anywhere on the page (Cmd+V) — no textarea focus required. Or drag-and-drop onto the Capture card (orange highlight on hover). Images are base64-encoded and sent with `/ingest`. Tap a thumbnail to view full-size; tap `+` to add more.

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
