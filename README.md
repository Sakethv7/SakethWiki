# SakethWiki

A personal knowledge system for capturing things learned in the wild — X/Twitter bookmarks, blogs, course snippets, Instagram tech content — and building a compounding Obsidian vault.

**Vault location:** `/Users/sakethv7/Documents/SakethWiki/`

---

## Stack

- **Backend:** FastAPI + Python, running on port 8001
- **Frontend:** React + Vite, Tailwind CSS (CDN), running on port 5173
- **LLMs:** claude-sonnet-4-6 (extraction) + claude-haiku-4-5 (formatting + chat)
- **Storage:** Flat Markdown files — no database, no embeddings

---

## Setup

### 1. Install Python dependencies

```bash
cd /Users/sakethv7/Sakethwiki
pip install -r requirements.txt
```

### 2. Set environment variable

```bash
export ANTHROPIC_API_KEY=your_key_here
export VAULT_PATH=/Users/sakethv7/Documents/SakethWiki
```

Or add both to your shell profile (`~/.zshrc`).

### 3. Start the backend

```bash
cd /Users/sakethv7/Sakethwiki/backend
python main.py
# or: uvicorn main:app --host 0.0.0.0 --port 8001 --reload
```

### 4. Install frontend dependencies and start

```bash
cd /Users/sakethv7/Sakethwiki/frontend
npm install
npm run dev
```

Frontend will be at: http://localhost:5173

---

## API Reference

| Method | Path | Description |
|--------|------|-------------|
| POST | `/ingest` | Fetch URL or accept text/image, extract metadata, stage to queue |
| GET | `/queue` | List all pending HITL items |
| POST | `/approve/{id}` | Approve or reject a queued item |
| POST | `/chat` | Chat with your wiki (keyword-matched RAG) |
| GET | `/pages` | List all concept pages with metadata |
| GET | `/page/{name}` | Full content of a concept page |

### POST /ingest

```json
{
  "url": "https://example.com/article",
  "text": "optional extra text",
  "image_base64": "optional base64 PNG"
}
```

### POST /approve/{id}

```json
{
  "approved": true,
  "redirect_note": "optional note"
}
```

### POST /chat

```json
{
  "message": "What do I know about RAG?",
  "history": []
}
```

---

## iPhone Access via Tailscale

1. Install [Tailscale](https://tailscale.com) on your Mac and iPhone
2. Log in with the same account on both devices
3. Find your Mac's Tailscale IP: `tailscale ip -4` (e.g. `100.x.y.z`)
4. Edit `frontend/src/App.jsx`, line 3:
   ```js
   const API = "http://100.x.y.z:8001";  // your Mac's Tailscale IP
   ```
5. Rebuild the frontend: `npm run build && npm run preview`
6. Access on iPhone: `http://100.x.y.z:4173`

The backend (`0.0.0.0:8001`) is already bound to all interfaces and will be reachable on Tailscale.

---

## Running the End-to-End Test

```bash
cd /Users/sakethv7/Sakethwiki
# Make sure backend is running first
python test_ingest.py
```

The test will:
1. Ingest Lilian Weng's AI Agents blog post
2. Show the diff preview
3. Auto-approve and write to vault
4. Confirm the `.md` file exists and print its content
5. Chat: "what do I know about AI agents?"

---

## Vault Structure

```
/Users/sakethv7/Documents/SakethWiki/
└── _wiki/
    ├── concepts/       ← RAG.md, agents.md, kv-cache.md ...
    ├── sources/        ← one .md per URL ingested
    ├── index.md        ← auto-maintained index
    └── log.md          ← append-only operation log
```

Concept pages compound — each new ingest for the same concept **appends** a new `##` section. Pages are never overwritten.

---

## Architecture

See [CONCEPTS.md](CONCEPTS.md) for full architecture diagram, data flow, model assignment rationale, and design decisions.
