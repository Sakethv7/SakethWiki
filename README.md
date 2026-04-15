# SakethWiki

A personal knowledge system for capturing things learned in the wild — X/Twitter bookmarks, blogs, course snippets, screenshots — and building a compounding, evolving Obsidian vault.

**Vault location:** `~/SakethVault` (home dir — avoids macOS TCC restrictions for dock-launched apps)

---

## Stack

- **Backend:** FastAPI + Python, running on port 8001
- **Frontend:** React + Vite, Tailwind CSS (CDN), running on port 5173
- **LLMs:** `claude-sonnet-4-5` (extraction) · `claude-haiku-4-5` (evolution analysis, chat, formatting)
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
| POST | `/health-check` | Analyse vault for issues and suggest fixes |
| POST | `/consolidate` | Merge two concept pages into one |
| POST | `/ingest-text` | Ingest plain text directly (no URL fetch) |

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

## Running Tests

```bash
cd backend
source venv/bin/activate
pytest tests/
```
