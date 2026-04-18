# SakethWiki — System Architecture

## High-Level Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                      Frontend (React/Vite)                      │
│                     http://localhost:5173                       │
│  ┌────────────┬────────────┬────────────┬──────────────────┐   │
│  │  Capture   │   Chat     │   Browse   │  Health Check    │   │
│  │            │            │            │  (/lint + apply) │   │
│  └────────────┴────────────┴────────────┴──────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────┐
│                  Backend API (FastAPI/Python)                   │
│                  http://localhost:8001                          │
│  ┌──────────────┬──────────────┬──────────────┬──────────────┐  │
│  │  Ingestion   │  Vault Ops   │  Chat & RAG  │ Health Check │  │
│  │  (/ingest)   │  (/lint)     │  (/chat)     │  Automation  │  │
│  │              │  (/add-link) │              │  (/add-link, │  │
│  │              │  (/create-   │              │   /create-   │  │
│  │              │   stub)      │              │   stub)      │  │
│  └──────────────┴──────────────┴──────────────┴──────────────┘  │
└──────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────┐
│                  Vault (Markdown + JSON)                        │
│              ~/SakethVault/_wiki/concepts/*.md                  │
│              ~/SakethVault/_wiki/meta/traces.jsonl              │
│              ~/SakethVault/_wiki/meta/system-insights.md        │
└──────────────────────────────────────────────────────────────────┘
```

---

## Core Subsystems

### 1. Ingestion Pipeline

**Entry points:** `/ingest`, `/ingest-direct`, `/queue-url`

**Flow:**
```
URL/Text/Image
    ↓
BeautifulSoup parse (URL) + Claude Sonnet extract
    ↓
Queue item: { id, title, summary, tags, suggested_page, wikilinks, diagram? }
    ↓
Human Review (HITL Queue)
    ↓
Approve → wiki_writer.py → Atomic file write → Vault + Trace
```

**Key design:** Zero LLM for parsing (BeautifulSoup only). Full LLM only for semantic extraction. All writes are atomic (write-to-temp → rename).

### 2. Self-Learning Loop

**Entry point:** `/analyze-traces` (weekly auto or on-demand)

**Flow:**
```
traces.jsonl (all approve/reject history)
    ↓
Claude Sonnet reads last 100 traces
    ↓
Identifies patterns:
  - Tag confusion (what gets corrected most often?)
  - Duplicate signals (what topics produce duplicates?)
  - Page routing mistakes (suggested page ≠ final page)
    ↓
Writes: system-insights.md
  - Prompt hints (specific corrections to auto-inject)
  - Architectural recommendations
  - Tag vocabulary suggestions
    ↓
Next /ingest → reads Prompt Hints section → injects into extraction prompt
```

**Cost:** ~$0.10-0.30/week (one Sonnet call). Trace logging is free (file append).

### 3. Vault Health & Automation

**Entry point:** `GET /lint` → Frontend Browse tab "Health" button

**Architecture:**

```
┌─ /lint (GET) ─────────────────────────────────────────┐
│ Scans entire vault with Claude Sonnet:               │
│ - semantic inconsistencies (2+ page conflicts)        │
│ - missing connections (contextual gaps)               │
│ - suggested articles (concepts with no pages)         │
│ - orphaned pages (no incoming links)                  │
│ Returns: health_score, category_scores, issues       │
└────────────────────────────────────────────────────────┘
     ↓
┌─ Frontend Health Check UI ──────────────────────────────┐
│ Shows 4 issue categories:                              │
│ ⚡ Quick Wins (checkboxes → auto-apply)                │
│ 🔗 Missing Connections (checkboxes → /add-link)       │
│ 📝 Suggested Articles (checkboxes → /create-stub)     │
│ ⚠️ Inconsistencies (checkboxes → /consolidate)        │
│ 🏝️ Orphaned Pages (manual review → "Mark done")       │
│                                                        │
│ localStorage persistence:                             │
│  ackedMap = {_healthKey(text): {status, timestamp}}   │
│  Survives page refresh + re-runs                      │
└────────────────────────────────────────────────────────┘
     ↓
┌─ Automation Endpoints ──────────────────────────────────┐
│ POST /add-link                                         │
│   {from_page, to_page} → insert [[to_page]] wikilink  │
│                                                        │
│ POST /create-stub                                      │
│   {slug, reason} → create minimal .md for later fill  │
│                                                        │
│ POST /consolidate                                      │
│   {primary_page, duplicate_page} → merge pages        │
└────────────────────────────────────────────────────────┘
     ↓
File modifications on disk + ackedMap updated
```

### 4. Chat & Knowledge Retrieval

**Entry point:** `/chat`

**Flow:**
```
User question
    ↓
Is it a knowledge query? (regex + keyword match)
    ↓
YES: find_relevant_pages() → parse_concept_page()
     Returns: knowledge_card {concept, understanding, related, evidence}
    ↓
NO: keyword_match_context() → raw markdown snippets
    ↓
Claude Haiku answers with context
    ↓
Return: {answer, knowledge_card?, sources}
```

**Optimization:** Pre-filtered context (keyword search → top 5 pages) before LLM. Haiku is sufficient for Q&A; Sonnet reserved for extraction.

---

## File & Data Structures

### Vault Layout

```
~/SakethVault/
├── _wiki/
│   ├── concepts/                     ← Evolving knowledge
│   │   ├── rag.md
│   │   ├── agents.md
│   │   └── ...
│   ├── sources/                      ← Immutable audit trail
│   │   ├── 2026-04-15-lilian-weng-agents.md
│   │   └── ...
│   ├── insights/                     ← Synthesized pages
│   ├── meta/
│   │   ├── traces.jsonl              ← Approval history
│   │   ├── system-insights.md        ← Weekly analysis output
│   │   ├── index.md                  ← Auto-rebuilt vault index
│   │   └── hitl_queue.json           ← Items awaiting review
│   └── standards.md                  ← Wiki standards (health check rules)
└── .obsidian/                        ← Obsidian config (optional)
```

### Concept Page Structure

```markdown
---
title: "Transformer Attention"
tags: [Attention, LLM, Deep-Learning]
entry_count: 5
last_updated: 2026-04-18
understanding_version: 3
last_evolution: 2026-04-18
---

> **Current understanding** 🟡
> Transformers use multi-head self-attention to weigh relevance
> of all input tokens in parallel, replacing RNN sequential processing.
> *— refined by "Attention is All You Need" · 2026-04-18*

## [Attention is All You Need](https://arxiv.org/pdf/1706.03762) · 2026-04-18
- Self-attention mechanism: Query × Key^T / √d × Value
- Multi-head: attention in multiple representation subspaces
- ...

## [Previous Source](url) · 2026-03-20
- ...
```

### Health Check Report (JSON)

```json
{
  "health_score": 61,
  "category_scores": {
    "agents": 72,
    "inference": 60,
    "memory": 45
  },
  "inconsistencies": [
    {
      "pages": ["agent-harness", "langgraph-react-agents"],
      "issue": "Conflicting definitions of agent architecture scope..."
    }
  ],
  "missing_connections": [
    {
      "from_page": "latent-briefing",
      "to_page": "phase-3-attention-and-transformers",
      "reason": "KV cache mechanics are foundational to latent-briefing..."
    }
  ],
  "suggested_articles": [
    {
      "title": "Autoregressive Generation",
      "reason": "Referenced in 3 pages but no dedicated concept page"
    }
  ],
  "orphaned_pages": ["unused-concept", ...]
}
```

### Trace Record (JSON Lines)

```json
{
  "ts": "2026-04-18T15:30:00",
  "url": "https://x.com/...",
  "source_type": "tweet",
  "approved": true,
  "suggested_page": "learning-agent-infrastructure",
  "final_page": "harness-hill-climbing",
  "page_corrected": true,
  "tags_suggested": ["Agentic"],
  "tags_final": ["Agents"],
  "tags_corrected": true,
  "was_duplicate": false
}
```

---

## Key Design Principles

### 1. **Vault-First, File-Based**
- All data is plain Markdown (Obsidian compatible)
- No database, no embeddings, zero infrastructure
- git-friendly — entire vault is versionable
- Portable — zip and move to any device

### 2. **Atomic Writes**
- Pattern: write to `.tmp` → `os.rename()` → done
- POSIX guarantees rename is atomic on same filesystem
- Prevents partial writes from corrupting files on crash

### 3. **Structured Frontmatter**
- YAML frontmatter stores metadata (tags, versions, counts)
- Enables filtering, sorting, and evolution tracking
- Version field drives understanding badge emoji progression

### 4. **LLM Where Rule-Based Fails**
- URL parsing: BeautifulSoup (zero LLM)
- Extraction: Sonnet (complex semantic reasoning)
- Evolution classification: Haiku (fast binary choice)
- Chat: Haiku (speed matters, pre-filtered context)

### 5. **Living Understanding Blocks**
- NOT append-only — single synthesized block at top
- Rewritten on each approval to reflect latest synthesis
- Source sections below are immutable evidence trail
- Badge emoji shows evolution count (🔵🟡🟠🔴⚪)

### 6. **Human-in-the-Loop (HITL)**
- All extractions staged in queue before vault write
- Humans can edit before approval
- Every correction feeds back to system-insights
- Zero junk in vault guaranteed

### 7. **Persistence Without Database**
- localStorage for front-end state (health check acked items)
- File system for all data (Markdown + JSON lines)
- Traces append-only (never modified, only appended)
- Index rebuilt on every write (zero stale state)

### 8. **Self-Healing Automation**
- Health check scans for structural issues
- Automation levels:
  - **Auto-fixable:** links, stubs, quick wins
  - **Auto-suggestible:** inconsistency merges
  - **Manual-reviewable:** orphaned pages
- Persistence via content-keyed hashing (survives re-runs)

---

## Performance & Costs

| Operation | Cost | Latency | Notes |
|-----------|------|---------|-------|
| `/ingest` (URL fetch + extract) | $0.02-0.05 | 5-15s | Sonnet extraction |
| `/chat` (Q&A) | $0.001-0.003 | 1-3s | Haiku answer |
| `/lint` (full vault scan) | $0.05-0.10 | 10-30s | Sonnet scanning all pages |
| `/add-link` (insert wikilink) | $0 | <100ms | File I/O only |
| `/create-stub` (new page) | $0 | <50ms | File I/O only |
| `/analyze-traces` (weekly) | $0.10-0.30 | 30-60s | Weekly Sonnet analysis |
| Trace logging | $0 | <1ms | File append only |

---

## Extensions & Future Work

### Potential Additions
- **Graph Visualization:** `/graph` endpoint returns relationship matrix for D3 rendering
- **Backlinks:** `/backlinks/{page}` shows all pages linking to a concept
- **Related Pages:** Expanded "See also" section auto-populated from missing connections
- **Multi-user Traces:** Per-user correction patterns for personalized extraction hints
- **Semantic Search:** Optional vector DB (Qdrant, Weaviate) for similarity-based retrieval
- **Export:** Generate Jekyll/Hugo sites from vault for sharing knowledge publicly

### Scaling Considerations
- **Vault Size:** Current design handles 100-500 concept pages easily. Beyond 1000, consider pagination in health checks.
- **LLM Costs:** Switch cheaper models for high-volume operations (e.g., Haiku for full vault scan if cost becomes issue).
- **Trace Storage:** Consider archiving old traces (>1 year) to `_archive/` once insights are extracted.
