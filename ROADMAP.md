# SakethWiki Roadmap

## 🎯 Top 10 Priorities (Next 4 Weeks)

### 1. **Personal Learning Dashboard** [1-2 days]
**What:** New tab showing learning metrics from last 30 days
**Includes:**
- Activity timeline (concepts added by date)
- Learning velocity (entries/week, concepts/week)
- Most-referenced tags (pie chart)
- Top sources (tweets, papers, blogs — frequency)
- "X new concepts this week" badge

**Why:** Metacognition—see your own learning patterns
**Implementation:**
- Add `GET /dashboard-stats` endpoint that reads traces + pages, aggregates stats
- Create Dashboard tab with simple charts (use existing chart library or Canvas)
- Query: count(entries) by date from traces.jsonl

---

### 2. **Understanding Maturity Scoring** [1 day]
**What:** Add maturity score (0-100) to each concept page
**Metrics:**
- Source count (more sources = higher confidence)
- Last updated recency (fresh = higher)
- Incoming links (more references = more important)
- Evolution count (updated multiple times = mature)
- Contradiction count (conflicting sources = lower)

**Why:** Know which concepts are rock-solid vs tentative
**Implementation:**
- Add `POST /calculate-maturity/{page}` endpoint
- Stores score in frontmatter: `understanding_maturity: 75`
- Display as meter on concept page (🟢 75/100)
- Run on every page write, cache result

**Formula:**
```python
score = (
  (source_count / max_sources) * 30 +
  (days_since_update < 30 ? 100 : decay) * 20 +
  (incoming_links / max_links) * 25 +
  (evolution_count / max_evolutions) * 15 +
  (1 - contradiction_count / total_sources) * 10
)
```

---

### 3. **Search on Browse Page** [2 hours]
**What:** Add client-side search/filter to Browse tab
**Includes:**
- Filter by keyword (in title + understanding block)
- Filter by tag (dropdown)
- Filter by maturity range (>=70)
- Sort by: name, updated, maturity, entry_count

**Why:** Can't find concepts in vault of 50+ pages
**Implementation:**
- Add `<input>` + filter logic to Browse component
- Debounce search (300ms)
- Highlight matches in results
- No backend change needed (filter locally)

---

### 4. **Quick Wins UI Polish** [3-4 hours, do in parallel]
Batch of small UX improvements:
- [ ] Add favicon (use initial "S" or wiki icon)
- [ ] Copy concept URL button (Cmd+C shortcut)
- [ ] "Random concept" button (serendipitous learning)
- [ ] Show last updated timestamp on concept page
- [ ] Word count on concept pages
- [ ] Mobile-responsive hamburger menu
- [ ] Toast notifications for approve/reject actions

**Why:** Feels more polished; addresses friction points
**Implementation:** Frontend-only changes, ~30min each

---

### 5. **Health Check Caching & Incremental Runs** [1-2 days]
**Current Problem:** Full vault scan takes 30s, costs $0.05-0.10 each time
**Solution:**
- Cache last `/lint` report in `_wiki/meta/lint-cache.json`
- Add timestamp + hash of pages list
- On next `/lint` call: if cache valid (<24h) and page list unchanged, return cached
- Button: "Force refresh health check" to bypass cache

**Why:** Users click health check multiple times; waste money + time
**Implementation:**
- Add `_load_lint_cache()` and `_save_lint_cache()`
- Check: `cached.timestamp > now - 24h AND cached.page_hash == current_hash`
- Store: `{timestamp, page_hash, report}`

**Cost Impact:** 99% faster second check; save ~$0.10/day on repeated runs

---

### 6. **Auto-Generated Summaries** [2-3 days]
**What:** POST /generate-summary/{page} returns structured summaries
**Generates:**
- One-liner (30 words)
- Paragraph (200 words)
- Study guide (5 self-test questions)
- Prerequisites (what to read first)
- Visual flowchart (mermaid diagram)

**Why:** Flashcard export, teaching others, quick refreshers
**Implementation:**
```python
@app.post("/generate-summary/{page}")
async def generate_summary(page: str):
  content = read_page(page)
  summaries = await client.messages.create(
    model="claude-opus-4-5",
    messages=[{
      "role": "user",
      "content": f"""For the concept page '{page}':
1. One-liner summary (30 words)
2. Paragraph explanation (200 words)
3. 5 self-test questions
4. Prerequisites to learn first
5. Mermaid diagram showing relationships
"""
    }]
  )
  return {
    "one_liner": ...,
    "paragraph": ...,
    "questions": [...],
    "prerequisites": [...],
    "diagram": "graph LR..."
  }
```

**Cost:** ~$0.01 per summary (Opus)

---

### 7. **Better Queue Management** [1-2 days]
**Current:** Just a list; hard to manage 50+ pending items
**Add:**
- Filter by source_type (URL, text, image)
- Filter by extraction confidence (high/medium/low)
- Bulk approve from trusted sources (auto-approve all tweets from @paulg)
- Snooze items ("revisit in 3 days")
- Queue stats: "73 pending | avg wait: 2.1 days"
- Reorder by date/confidence

**Why:** Queue becomes overwhelming without tools
**Implementation:**
- Store `source_type` in queue items
- Add extraction confidence score from Claude
- Checkboxes for bulk actions
- Snooze: store `snooze_until` in hitl_queue.json

---

### 8. **Understanding Evolution Timeline** [1 day]
**Current:** Badge shows "3 updates" but no detail
**Add:**
- Click badge → modal showing evolution history
- Each evolution row: "Refined by X on Y date from source Z"
- Show what changed (unified diff of understanding block)
- Confidence over time (did it ever get contradicted?)

**Why:** See how concepts evolved; trace back to sources
**Implementation:**
- Store evolution log in frontmatter: `evolution_history: [{date, type, source, diff}]`
- Or read from traces.jsonl (find all traces where final_page == this_page)
- Display in modal with timeline UI

---

### 9. **Concept Page Inline Editing** [2-3 days]
**Current:** View-only; edit requires opening file in editor
**Add:**
- "Edit" button appears on concept pages
- Inline editor for understanding block + See also section
- Preview markdown before saving
- Auto-commit to git with message "Updated via web UI"
- Revert button if needed

**Why:** Capture improvements without leaving app
**Implementation:**
- Add `POST /edit-page/{page}` endpoint
- Request: `{ updated_content: "..." }`
- Validates frontmatter, applies changes, git commits
- Returns: `{success, git_commit_sha}`

---

### 10. **Tag Hierarchy System** [2-3 days]
**Current:** Flat list of tags; confusion between "Agents" and "Agentic"
**Add:**
- Define tag ontology: `AI > LLM > Attention`, `AI > Agents > Multi-agent`
- Synonym mapping: `{Agentic, Agents, Multi-agent} → primary: Agents`
- Tag autocomplete suggests hierarchy
- Browse by tag shows all nested tags
- Migration: collapse duplicates with prompts

**Why:** Vault stays clean at scale; reduces confusion
**Implementation:**
- Create `_wiki/meta/tag-ontology.json`
- Structure: `{ "parent": ["child1", "child2"], "synonyms": ["alt1", "alt2"] }`
- On page write: normalize tags through synonym map
- Display hierarchy in Browse filter

---

## 📋 Implementation Sequence

### Week 1
1. ✅ Learning Dashboard (1-2 days) → get metacognition immediately
2. ✅ Search/filter on Browse (2 hours) → usability
3. Quick wins polish (3-4 hours in parallel) → feels better
4. ✅ Maturity scoring (1 day) → confidence signal

### Week 2
1. ✅ Health check caching (1-2 days) → save money & time
2. Better queue management (1-2 days) → handle growth
3. ✅ Auto-summaries (2-3 days) → new capability

### Week 3
1. ✅ Inline editing (2-3 days) → reduce friction
2. Evolution timeline (1 day) → better understanding
3. ✅ Tag hierarchy (2-3 days) → scale-ready taxonomy

### Week 4
1. Polish based on feedback
2. Bug fixes discovered during implementation
3. Start next batch

---

## ✅ Recently Shipped (Session — 2026-04-21)

| Feature | Notes |
|---------|-------|
| Recently Read | `POST /log-read` + `GET /recent-reads`, reads.jsonl, Dashboard section |
| Inline Concept Editing | `POST /edit-page/{name}`, EditModal with preview + git commit |
| Tag Normalizer | tag-ontology.json, `POST /normalize-tags`, `GET /tag-ontology`, auto-normalize on approve |
| Dashboard Redesign | GitHub-style 16×7 orange heatmap, compact stat row, source pill chips |
| iOS Shortcut Fast Path | ~20ms return, background asyncio extraction, "Extracting…" spinner, 3s polls |
| Image Paste & Drag-Drop | Document-level paste, drag-and-drop with orange highlight, thumbnail preview |
| Random Concept | `GET /random-concept`, 🎲 button in Browse |
| Study Summaries | `POST /generate-summary/{name}`, one-liner + Q&A + mermaid diagram modal |
| Tag Vault Cleanup Script | `normalize_vault_tags.py --dry-run` / apply |

---

## 🚀 Post-MVP Features

### Medium-Term (Month 2-3)
- Browser extension for 1-click capture
- Export to Anki/Quizlet JSON (spaced repetition)
- Twitter bookmark importer
- /generate-learning-path endpoint (structured curriculum)
- Obsidian vault sync (two-way)

### Long-Term (3-6 months)
- Published wiki mode (share vault as web site)
- Slack bot (ask vault from Slack)
- Collaborative learning mode (shared vaults)
- Mobile app (React Native)
- Community concept marketplace

---

## 📊 Success Metrics to Track

Once features launch, measure:
1. **Dashboard:** Do you open it weekly? What insights stick?
2. **Maturity scores:** Do they match your intuition?
3. **Search:** Can you find concepts faster?
4. **Health check:** Does caching speed it up noticeably?
5. **Summaries:** Do you export them? Use in Anki?
6. **Queue:** Manage 100+ pending items without stress?
7. **Editing:** Percent of updates done via web vs external editor?

---

## 🎨 Design Notes

### UI Consistency
- Keep palette: stone grays, minimal color (blue accents for interactivity)
- No modals for critical flows (use inline or sidebar)
- Loading states visible (skeleton screens, not spinners)
- Keyboard first (J/K navigation, Cmd+K for search)

### Performance Targets
- Dashboard loads < 500ms
- Search results < 100ms
- Page navigation < 200ms
- Health check (cached) < 50ms

### Testing Coverage
- Unit tests for: maturity calculation, tag normalization, evolution logic
- E2E tests for: ingest→approve→concept page flow
- Load test: health check with 1000 pages

---

## 💡 Open Questions

1. **Complexity:** Some features (tag hierarchy, evolution timeline) add complexity. Worth it?
2. **Monetization:** Export features → could charge for publishing/sharing. Interested?
3. **Collaboration:** Single-user now. Ever want to share vault with study partners?
4. **Mobile:** Web works on iPhone via Tailscale. Need native app?
5. **Community:** Ever want public concept marketplace (share/import others' knowledge)?

