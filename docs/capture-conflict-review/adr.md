# Capture Conflict Review — Decisions

Each ADR lists what was given up. A decision with no downside is not finished.

## ADR 1 — Compare at capture time, not at approve time

### Visual Level

```mermaid
flowchart TD
    A[Clip extracted] --> B{When do we compare?}
    B -- today: at approve --> C[User approves blind]
    C --> D[Writer may drop or blend text]
    B -- new: before queue --> E[Report stored on item]
    E --> F[User sees result, then decides]
    classDef new fill:#fde68a,stroke:#b45309,color:#000;
    class E,F new;
```

Yellow steps are new. The comparison moves ahead of the decision.

**Context.** `wiki_writer._analyze_evolution` already classifies a clip against its page (extends, refines, supersedes, duplicates, contradicts). It runs inside the approve call. The result is not shown first. A "duplicates" result drops the clip with no prompt.

**Options.** (a) Keep it at approve time and show a confirm dialog. (b) Run it when extraction finishes and store the result on the queue item. (c) Run it when you open the item.

**Choice.** (b). The queue can show a band badge on every row without extra clicks. The cost is paid once, in the background, not while you wait on a button.

**Consequences.** The report can go stale if a page changes before approval. `/approve` compares a page hash and asks for a refresh. The old approve-time classifier stays only as a fallback for items without a report.

**Given up.** Extraction takes a few seconds longer. A stored report can be wrong if the vault changes.

## ADR 2 — Bands come from a claim-level diff plus plain rules

### Visual Level

```mermaid
flowchart TD
    A[Clip claims and page text] --> D[Haiku labels each claim]
    D --> E{Valid JSON?}
    E -- no --> F[Band: unknown]
    E -- yes --> H{Same claim has a number not in its quote?}
    H -- yes --> I[Relabel it: changed]
    H -- no --> G[Rules turn labels into a band]
    I --> G
    classDef new fill:#fde68a,stroke:#b45309,color:#000;
    class D,E,F,G,H,I new;
```

Yellow steps are all new. The LLM only labels claims. Code checks the numbers and picks the band.

**Context.** A similarity score cannot find a conflict. Two clips on parallel scans score high even when one says 204 s and the other says 90 s. `consolidation.py` scores page pairs this way, and that is right for finding merge candidates. It is not right for judging a new fact.

**Options.** (a) Score only: jaccard plus slug similarity. (b) Ask the LLM for a band directly. (c) Ask the LLM for a label per claim, then derive the band with fixed rules.

**Choice.** (c). Per-claim labels are `same`, `new`, `changed`, `conflicts`. Rules: any `changed` or `conflicts` gives `conflict`. Else any `new` gives `overlap`. Else all `same` gives `duplicate`. This is easy to test and the UI can show the labels as evidence. Two code checks guard the labels. A `same` claim needs an exact quote from the page, or it becomes `new`. A `same` claim whose numbers are not all in that quote becomes `changed`.

**Amendment after calibration (2026-10-06).** The first draft of this ADR had a token-overlap gate: if 90% of the clip's terms were on the page, call it `duplicate` with no LLM call. The calibration run showed that this marks a clip with one doubled number as a duplicate. 10 of 10 constructed conflicts were skipped. That is the exact failure this feature must prevent. The gate is removed. Token overlap now only picks the target page. The cost is one Haiku call for clips that really are duplicates.

**Consequences.** The band is explainable: you can point at the claim that caused it. The one threshold left is the 0.35 minimum overlap for a target page that the extractor did not name. The gate-only run puts 10 distinct pairs at 0.19 to 0.29 and 10 overlap pairs at 0.56 to 0.75, so 0.35 sits in the gap.

**Given up.** An extra Haiku call on every clip with a close page. Claim extraction can split or merge claims differently from run to run, so the same pair may get a different count of claims.

## ADR 3 — No automatic merge. Only `duplicate` acts alone, and only to skip

### Visual Level

```mermaid
flowchart TD
    A[Band known] --> B{Is it duplicate?}
    B -- yes --> C[Skip and link to page]
    C --> D[Add anyway button]
    B -- no --> E[Show side-by-side]
    E --> F[User picks append, replace, or keep both]
    classDef new fill:#fde68a,stroke:#b45309,color:#000;
    class C,D,E,F new;
```

Yellow steps are new. Everything except the duplicate skip waits for you.

**Context.** You asked whether the app should merge similar clips and leave different ones behind. Your first ingest was partly wrong. An automatic merge would copy that error into a page with the new text. You would find out later, if at all.

**Options.** (a) Auto-merge overlap, ask on conflict. (b) Ask for everything except duplicate. (c) Ask for everything.

**Choice.** (b). `duplicate` skips because the page already holds every claim, so nothing is lost. The skip shows a notice, links the page, and offers "Add anyway". A clip that is "too different" becomes a new page. It is never dropped.

**Consequences.** `overlap` clips need one click. This is the price of never changing a trusted page silently.

**Given up.** The fully hands-off flow you described. If review feels slow after a trial, a later ADR can add auto-append for `overlap` with a high recommendation score. That needs calibration data first.

## ADR 4 — `replace` archives the old page first

### Visual Level

```mermaid
flowchart TD
    A[User picks replace] --> B[Copy old page to meta/replaced]
    B --> C{Copy worked?}
    C -- no --> D[Stop, show error, page unchanged]
    C -- yes --> E[Write new page]
    E --> F[Log replace in log.md]
    classDef new fill:#fde68a,stroke:#b45309,color:#000;
    class A,B,C,D,E,F new;
```

Yellow steps are all new.

**Context.** When a clip contradicts a page and the clip is right, you need to overwrite the page. Overwrite with no copy is the failure mode you want to avoid.

**Options.** (a) Overwrite in place. (b) Archive then overwrite. (c) Use git history in the vault.

**Choice.** (b). The old page goes to `_wiki/meta/replaced/<slug>-<timestamp>.md`. The vault is not a git repo, so (c) is not available.

**Consequences.** Archived pages are not indexed or linked. They stay on disk until you delete them.

**Given up.** Disk space. Backlinks to the old page keep pointing at the new page, which may surprise you.

## ADR 5 — Treat the processed-clips ledger as a cache of the vault

### Visual Level

```mermaid
flowchart TD
    A[Same clip pasted again] --> B{Ledger entry found?}
    B -- no --> C[Process as new]
    B -- yes --> D{Does its page still exist?}
    D -- yes --> E[Block: already processed]
    D -- no --> C
    classDef changed fill:#bfdbfe,stroke:#1d4ed8,color:#000;
    class D changed;
```

Blue marks the changed step. This fix ships in PR 7.

**Context.** The ledger is a second record of what is in the wiki. When you delete a page, the ledger disagrees with the vault.

**Choice.** An entry blocks only while its `file_written` page exists. This is PR 7. After this feature, the `duplicate` band covers repeated clips by content, so the ledger matters less.

**Consequences.** If the ledger and the vault disagree, the vault wins.

**Given up.** One file-exists check per matching ledger entry. Entries with no `file_written` still block.

## ADR 6 — Show claims, decide per page

### Visual Level

```mermaid
flowchart TD
    A[Report has claim verdicts] --> B[Modal lists each claim with a chip]
    B --> C[User reads the evidence]
    C --> D{Pick one action}
    D --> E[append]
    D --> F[replace]
    D --> G[keep both]
    D --> H[skip]
    classDef new fill:#fde68a,stroke:#b45309,color:#000;
    class B,C,D,E,F,G,H new;
```

Yellow steps are all new.

**Context.** The best control is per-claim: keep claim 2, drop claim 3. That needs an editor for page text and a way to write a partial section.

**Choice.** Show per-claim evidence. Take one action per clip. If one claim is wrong, you can edit the clip text in the existing review editor before approving.

**Consequences.** A clip with 5 good claims and 1 bad claim needs an edit step.

**Given up.** Fine control. Revisit if you hit this often.

## Open questions

1. **Labels to correct.** `calibration_pairs.json` holds 40 pairs with proposed labels (10 each: duplicate, conflict, overlap, distinct). Open it, fix any `label` that is wrong, and set `reviewed` to true. Then run `python backend/calibrate_compare.py`. The 10 conflict pairs are constructed (one number doubled). The 10 overlap pairs are weak labels: they are real clips against a related page, and some may be duplicate or conflict.
2. **Call sites.** There are 4 extraction paths (`/ingest`, `/ingest-markdown`, `/queue-url`, clip staging). `docs/capture-queue` proposes one worker that would reduce this to 1. That document is still "Proposed". Should this change wait for it, or hook into all 4 now? I recommend hooking into all 4 through one helper.
3. **Contradiction with the code.** `wiki_writer._analyze_evolution` can still drop a clip as "duplicates" at approve time, even after you chose `append`. This design passes your resolution into the writer so the writer does not re-judge. Confirm that is the right behavior.
4. **Explainer ladder levels 3 and 4** (HTML explainer and narrated video) are not built yet. They show a flow over time, so they are required. I plan to build them after you approve this design, so I do not rebuild them if the design changes. Say if you want them first.
5. **Ledger removal.** Should the ledger be deleted after this ships? Not decided here.
