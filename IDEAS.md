# SakethWiki Product Analysis: Ideas & TODOs

## Current Feature Inventory

### ✅ IMPLEMENTED
**Capture & Ingestion:**
- URL fetch + extraction (BeautifulSoup + Sonnet)
- Text/image direct ingestion
- HITL queue with human review before vault writes
- Auto-suggested page routing

**Knowledge Management:**
- Concept pages with YAML frontmatter + living understanding blocks
- Evolution classification (extends, refines, supersedes, contradicts, duplicate)
- Source attribution with links
- Tag classification system

**Discovery & Navigation:**
- Browse vault with folder filtering
- Search by keyword
- Backlinks (/backlinks/{page})
- Related pages suggestions (/follow-up/{page})
- Knowledge graph visualization (/graph)
- Chat Q&A with context retrieval

**Quality & Health:**
- Vault linting with health score (0-100)
- Issue detection: inconsistencies, missing connections, orphaned pages
- Auto-fix automation: add-link, create-stub, consolidate
- Understanding evolution badges (🔵🟡🟠🔴⚪)

**Learning Loop:**
- Approval/rejection trace logging (traces.jsonl)
- Weekly analysis with pattern detection
- Auto-injected prompt hints
- Tag confusion detection

### ❌ GAPS & OPPORTUNITIES

---

## Category 1: Analytics & Insights

### 1.1 Knowledge Graph Analytics
**Current:** Basic /graph endpoint returning raw data
**Missing:**
- [ ] Network density metrics (how interconnected?)
- [ ] Cluster detection (what topic areas are isolated?)
- [ ] Path suggestions ("to understand X, you should read Y→Z→A")
- [ ] Knowledge gaps visualization (dark spots in graph)
- [ ] Citation importance ranking (pages that enable understanding of others)

**Value:** Identifies where to invest learning effort; surfaces critical foundational concepts

### 1.2 Personal Learning Dashboard
**Current:** Individual tabs (ingest, chat, browse)
**Missing:**
- [ ] "Last 30 days" activity timeline (what topics I've been learning)
- [ ] Learning velocity chart (entries/week, concepts/week)
- [ ] Most-referenced concepts (what appears in my sources most?)
- [ ] Tag coverage heatmap (which tags have deep vs shallow knowledge?)
- [ ] Reading time estimates per page
- [ ] Spaced repetition hints ("you haven't reviewed KV-cache in 45 days")

**Value:** Metacognition — understand your own learning patterns; identify what's sticking vs what needs reinforcement

### 1.3 Content Quality Metrics
**Current:** Evolution count and last_updated timestamp
**Missing:**
- [ ] Understanding maturity score (0-100 based on: sources, updates, cross-references)
- [ ] Evidence quality (link to original research vs tweets vs blogs)
- [ ] Contradiction count (how many conflicting sources exist for this concept?)
- [ ] Age-to-update ratio (stale knowledge detector)
- [ ] Completeness estimate ("75% documented, 25% gaps")

**Value:** Know which concepts are well-understood vs tentative; prioritize what to learn more about

---

## Category 2: AI & Automation Enhancements

### 2.1 Auto-Generated Summaries & Study Guides
**Current:** Static concept pages
**Missing:**
- [ ] 30-second summary (one sentence)
- [ ] 2-minute explanation (paragraph)
- [ ] Study guide (5-10 questions for self-test)
- [ ] Visual explanation (auto-generate diagrams/flowcharts)
- [ ] "Explain like I'm 5" version
- [ ] Prerequisite map ("learn these 3 concepts first")

**Implementation:** POST /generate-summary triggered on approval or manual request

**Value:** Accelerate learning; export to flashcards (Anki, Quizlet); teach others

### 2.2 Smart Cross-Linking
**Current:** Missing connections detected but must be manually applied
**Missing:**
- [ ] Semantic similarity scoring (beyond keyword matching)
- [ ] Auto-apply threshold for high-confidence missing connections
- [ ] Bidirectional linking suggestions ("if A→B, should B→A?")
- [ ] Relationship type detection (prerequisite, refines, contradicts, similar)
- [ ] Prevent redundant links (A→C and A→B→C, suggest removing A→C)

**Value:** Faster health check resolution; less manual maintenance

### 2.3 Duplicate Detection Improvement
**Current:** Marked as "duplicate" in traces, but no action taken
**Missing:**
- [ ] Auto-suggest merge when 90%+ semantic overlap
- [ ] Consolidation diff viewer (show what would merge)
- [ ] Related vs duplicate distinction
- [ ] Merge simulation (show combined understanding)

**Value:** Keep vault clean without manual overhead

### 2.4 Personalized Extraction Hints Beyond Tags
**Current:** Prompt hints cover tag confusion + pattern detection
**Missing:**
- [ ] Page routing patterns (when user routes to X, it usually means Y)
- [ ] Source quality preferences (prioritize papers > blogs > tweets based on history)
- [ ] Writing style preferences (more technical vs conversational)
- [ ] Topic-specific domain rules ("in ML papers, always extract mathematical notation")

**Value:** Better-aligned extractions over time; fewer corrections needed

---

## Category 3: UX & Workflow

### 3.1 Faster Capture Workflow
**Current:** Paste URL → wait for extraction → review → approve
**Missing:**
- [ ] Cmd+K quick capture modal (anywhere in app)
- [ ] Drag-to-capture (drag URL/text into floating widget)
- [ ] Browser extension for 1-click capture (send to queue)
- [ ] Bulk ingest (paste 5 URLs at once, queue them all)
- [ ] Quick-approve flow (approve without editing for trusted sources)
- [ ] Fallback capture (if extraction fails, just save raw content)

**Value:** Remove friction; capture becomes instantaneous

### 3.2 Better Queue Management
**Current:** Simple list of pending items
**Missing:**
- [ ] Filter by: date, source type, routing confidence, tag
- [ ] Bulk actions (approve all from trusted source, reject all low-confidence)
- [ ] Priority sorting (by suggested importance, extraction confidence)
- [ ] Snooze items ("revisit this later")
- [ ] Comments on queue items ("remember to add section X before approving")
- [ ] Queue stats ("97 pending, avg wait 3 days")

**Value:** Queue doesn't become overwhelming; better context management

### 3.3 Concept Page Improvements
**Current:** Static markdown rendering
**Missing:**
- [ ] Edit button (inline editing, commit back to file)
- [ ] Related pages sidebar (auto-populated from backlinks + missing connections)
- [ ] "Last updated" with author/trace context (came from which source?)
- [ ] Understanding evolution timeline (click badge to see history)
- [ ] Suggestion chips below understanding (Claude suggests: "consider adding...")
- [ ] "You added this X days ago" — reminds you to review

**Value:** Pages become more interactive; easier to improve without leaving page

### 3.4 Export & Sharing
**Current:** Vault only visible locally (or via Tailscale)
**Missing:**
- [ ] Export concept as: Markdown, PDF, image, HTML
- [ ] Share as: static web page, read-only link, Obsidian vault file
- [ ] Generate study guide deck (export to Anki JSON)
- [ ] Tweet thread generator (convert concept to tweet series)
- [ ] Blog post generator (concept → publishable article)
- [ ] Bibliography export (BibTeX, APA, MLA)

**Value:** Monetization opportunity; share learning with community; better note portability

---

## Category 4: Performance & Scale

### 4.1 Vault Scalability
**Current:** Works well for <500 pages; health check scans entire vault with Sonnet
**Missing:**
- [ ] Incremental health check (only check pages modified since last run)
- [ ] Health check caching (cache report for 24h, force-refresh button)
- [ ] Lazy-load page previews (don't render 100 pages, lazy-load on scroll)
- [ ] Search indexing (build inverted index once, search locally)
- [ ] Archive old pages (move traced content to _archive/ to speed up health check)

**Value:** Scale to 5000+ pages without degradation

### 4.2 Trace Storage Optimization
**Current:** traces.jsonl grows unbounded
**Missing:**
- [ ] Periodic archival (compress traces older than 90 days)
- [ ] Sampling for old traces (keep 10% of traces after 1 year)
- [ ] Summary statistics file (pre-computed insights for old periods)
- [ ] Incremental analysis (only analyze new traces since last run)

**Value:** Keep vault light; faster analysis runs

### 4.3 Frontend Performance
**Current:** React re-renders on all page loads
**Missing:**
- [ ] Virtualization for large lists (Browse page with 500 concepts)
- [ ] Image optimization (lazy-load diagrams)
- [ ] Code splitting (separate health check UI bundle)
- [ ] Debouncing on search/filter
- [ ] ServiceWorker for offline mode (read-only access to local vault)

**Value:** Feels snappier on slower networks

---

## Category 5: Integrations & Ecosystem

### 5.1 Source Integrations
**Current:** Manual paste, URL fetch
**Missing:**
- [ ] Twitter/X bookmarks auto-import (OAuth integration)
- [ ] Readwise integration (highlights from articles → sources)
- [ ] Notion sync (read from Notion database → ingest)
- [ ] Pocket integration (read later → ingest)
- [ ] Academic paper fetch (ArXiv, Semantic Scholar)
- [ ] Podcast transcript import (with timestamps)

**Value:** Reduce capture friction; passive learning aggregation

### 5.2 Output Integrations
**Current:** None (vault is local)
**Missing:**
- [ ] Obsidian vault sync (two-way sync with Obsidian)
- [ ] Roam Research sync
- [ ] MindNode mindmap export (concept → mindmap)
- [ ] Slack bot (ask vault questions in Slack)
- [ ] Email digest ("5 concepts you haven't reviewed in 30 days")
- [ ] Spaced repetition app sync (AnkiDroid, Quizlet)

**Value:** Vault becomes hub; reach people where they are

### 5.3 Community Features
**Current:** Solo knowledge base
**Missing:**
- [ ] Publish mode (share vault as public wiki)
- [ ] Collaborative notes (real-time multi-user editing)
- [ ] Concept marketplace (find & import curated concepts from others)
- [ ] Learning groups (sync knowledge with study partners)
- [ ] Version history & diffs (git-like tracking)

**Value:** Network effects; learn from others' vaults

---

## Category 6: Technical Debt & Robustness

### 6.1 Error Handling & Recovery
**Current:** Some error handling, but gaps remain
**Missing:**
- [ ] Graceful degradation if LLM is down (use cached extraction fallback)
- [ ] Retry logic with exponential backoff (transient API failures)
- [ ] File corruption detection (validate YAML frontmatter on load)
- [ ] Vault backup on every write (automatic git commits)
- [ ] Audit trail (who changed what, when)

**Value:** Production-ready reliability

### 6.2 Testing
**Current:** Minimal test coverage
**Missing:**
- [ ] Unit tests for wiki_writer.py (page creation, evolution)
- [ ] Integration tests for ingestion pipeline
- [ ] E2E tests for full workflows
- [ ] Load tests (health check with 5000 pages)
- [ ] Data migration tests (version upgrades)

**Value:** Safe refactoring; confidence in changes

### 6.3 Observability
**Current:** Logs go to stdout
**Missing:**
- [ ] Structured logging (JSON with context)
- [ ] Metrics (API latency, queue depth, LLM token usage)
- [ ] Error rate dashboard
- [ ] Performance profiling (slow endpoints)
- [ ] Usage analytics (what features do you use most?)

**Value:** Debug issues faster; optimize based on data

---

## Category 7: Concept-Specific Ideas

### 7.1 Evolution Tracking
**Current:** Badge shows count, but no timeline
**Missing:**
- [ ] Interactive evolution history (click badge → see all states)
- [ ] Change diff (what changed from last version?)
- [ ] Why it changed (which source drove the update?)
- [ ] Confidence over time (was it ever contradicted?)

**Value:** Better understanding of how knowledge evolved

### 7.2 Tag System Improvements
**Current:** Free-form tags with classifier
**Missing:**
- [ ] Tag hierarchy (AI > LLM > Attention)
- [ ] Synonym management (Agentic, Agents, Multi-agent → unify)
- [ ] Tag autocomplete (smart suggestions as you type)
- [ ] Tag deprecation (mark old tags, migrate to new ones)
- [ ] Ontology visualization (how tags relate to each other)

**Value:** Cleaner taxonomy; better organization at scale

### 7.3 Reading Path Generation
**Current:** Follow-up suggestions are reactive
**Missing:**
- [ ] "Learn X in 30 minutes" curated path (5-page sequence)
- [ ] "From beginner to expert" learning path (10 stages)
- [ ] Difficulty levels (intro, intermediate, advanced concepts)
- [ ] Prerequisite mapping (what must you read first?)
- [ ] Learning outcomes (after reading path, you'll understand Y)

**Value:** Onboarding; teaching mode; structured learning

---

## Category 8: Quick Wins (High ROI, Low Effort)

### 8.1 Low Hanging Fruit
- [ ] Add favicon (currently blank)
- [ ] Add timestamp to health check report ("last scan: 2h ago")
- [ ] Search bar on Browse page (filter concepts client-side)
- [ ] Copy page URL button
- [ ] "Random concept" button (serendipitous learning)
- [ ] Dark mode toggle
- [ ] Concepts per tag filter view
- [ ] Display word count on concept pages
- [ ] "Print to PDF" endpoint for concepts
- [ ] Health check email digest option

**Effort:** 1-2 hours each | **Value:** immediate polish

### 8.2 Quality-of-Life Improvements
- [ ] Auto-save drafts in queue (don't lose edits if page refreshes)
- [ ] Keyboard shortcuts (J/K to navigate queue, ? for help)
- [ ] Undo for mark-as-done (recovered from accidental dismissals)
- [ ] Concept page view tracking (know which pages you visit most)
- [ ] "New concepts this week" summary
- [ ] Mobile-responsive improvements (hamburger menu, better spacing)
- [ ] Loading skeletons (less jarring waits)
- [ ] Toast notifications for actions (better feedback)

**Effort:** 2-4 hours each | **Value:** noticeably better UX

---

## Priority Framework

**High Impact + Low Effort** (Do First):
1. Dashboard with learning stats
2. Quick wins (favicon, search, random page)
3. Better queue management (filter, bulk actions)
4. Understanding maturity scores

**High Impact + Medium Effort** (Do Next):
1. Auto-generated summaries
2. Smart cross-linking automation
3. Browser extension for capture
4. Health check caching/incremental
5. Tag hierarchy system

**High Impact + High Effort** (Plan For):
1. Community features / published wikis
2. Source integrations (Twitter, Readwise)
3. Observability & monitoring
4. Mobile app (React Native?)

**Nice-to-Have** (Backlog):
1. All export formats
2. Real-time collaboration
3. Voice note ingestion
4. Video transcript support
