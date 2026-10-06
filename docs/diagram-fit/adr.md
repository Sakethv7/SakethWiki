# Diagram Fit — Decisions

Each ADR lists what was given up.

## ADR 1 — Rewrite the stored diagram, not only the display

### Visual Level

```mermaid
flowchart TD
    A[Wide diagram in a page] --> B{Where can we fix it?}
    B -- app display only --> C[Obsidian still shrinks it]
    B -- stored source --> D[App and Obsidian both get a narrower diagram]
    classDef new fill:#fde68a,stroke:#b45309,color:#000;
    class D new;
```

Yellow marks the chosen path.

**Context.** You open wide diagrams in Obsidian because the app shows them too small. Obsidian draws the text stored in the page. Nothing the app does at display time reaches Obsidian.

**Options.** (a) Fix only how the app draws diagrams. (b) Fix the stored source. (c) Both.

**Choice.** (c), with (b) as the main fix. A narrower diagram helps both viewers. The app display change (ADR 5) covers diagrams that cannot be made narrow.

**Consequences.** The page text changes. Old pages are changed by a one-time script with a backup (ADR 4).

**Given up.** The diagram in the page is no longer exactly what the LLM or you wrote.

## ADR 2 — A rule decides the direction, calibrated on measured data

### Visual Level

```mermaid
flowchart TD
    A[Left-to-right flowchart] --> B{Subgraphs, or fewer than 4 nodes?}
    B -- yes --> C[Keep direction]
    B -- no --> D[Switch to top to bottom]
    D --> E[Offline check measures the real result]
    classDef new fill:#fde68a,stroke:#b45309,color:#000;
    class B,C,D,E new;
```

Yellow steps are all new.

**Context.** The server cannot render Mermaid, so it cannot measure a width before saving. I measured all 106 left-to-right flowcharts in your vault, with and without the switch. The switch made 75 of them at least 10% narrower. It left 31 the same or wider. Almost every failure has subgraphs: with a subgraph, the top-to-bottom layout often gets wider.

**Options.** (a) Switch every left-to-right flowchart. (b) Switch by node count. (c) Switch only when there are no subgraphs and at least 4 nodes. (d) Render in a browser at write time.

**Choice.** (c). On the vault it switches 81 diagrams. 71 get narrower and 10 do not (88%). Node count alone did not predict anything: a threshold of 3 to 6 nodes got 70 to 75 right and 30 to 31 wrong. Option (d) would be exact but needs a browser inside the server. The offline check (`measure_diagrams.py`) gives exact numbers for the migration and for tuning the rule.

**Consequences.** For the vault migration, the check restores any page where the result is wider, so the 12% miss does not apply there. For new diagrams, about 1 in 8 switched diagrams may get wider. The scroll and Expand view handles those.

**Given up.** Exactness at ingest time. A diagram with subgraphs that is too wide stays too wide.

## ADR 3 — Top to bottom is the default for long chains

### Visual Level

```mermaid
flowchart TD
    A[Long chain, left to right] --> B[Becomes top to bottom]
    B --> C[Taller, narrower]
    C --> D[Scrolls down, not shrunk]
    classDef new fill:#fde68a,stroke:#b45309,color:#000;
    class B,C,D new;
```

Yellow steps are new.

**Context.** A tall diagram scrolls down at full size. A wide diagram is shrunk. On the 100 left-to-right flowcharts wider than 700 px, the switch brings 59 to 700 px or less. The median width of the 106 drops from 1095 px to 572 px.

**Options.** (a) Keep left to right and rely on scrolling. (b) Switch long chains to top to bottom. (c) Wrap labels only.

**Choice.** (b), plus label wrapping. Labels longer than 28 characters get a line break at a space near 24 characters. Wrapping alone cut little: the count below 75% size went from 111 to 94. The switch alone went to 59, and both together to 52.

**Consequences.** Reading order changes from left to right to top to bottom.

**Given up.** The wide, flat look of the original. Taller nodes from wrapped labels.

## ADR 4 — The vault rewrite is a dry run first, with a backup and a restore

### Visual Level

```mermaid
flowchart TD
    A[Run dry run] --> B[Read the report]
    B --> C[Run apply]
    C --> D[Copy each page to the backup folder]
    D --> E{Copy worked?}
    E -- no --> F[Stop, no page changed]
    E -- yes --> G[Rewrite diagrams in the page]
    G --> H[Run verify]
    H --> I{Parses and not wider?}
    I -- no --> J[Restore that page]
    I -- yes --> K[Keep]
    classDef new fill:#fde68a,stroke:#b45309,color:#000;
    class A,B,C,D,E,F,G,H,I,J,K new;
```

Yellow steps are all new.

**Context.** The vault is not a git repository. A page rewritten wrongly cannot be undone from history. Some pages may be notes you wrote by hand.

**Options.** (a) Rewrite in place. (b) Rewrite with a backup. (c) Write fitted copies next to the originals.

**Choice.** (b). `--dry-run` is the default and writes nothing. It lists each page and the planned change. `--apply` copies each page to `_wiki/meta/diagram-backup/<timestamp>/` before it changes it. `--restore <timestamp>` puts the backed-up pages back. Only text inside a fenced `mermaid` block can change.

**Consequences.** Backups stay until you delete them.

**Given up.** Disk space. A confirm step before the rewrite.

## ADR 5 — The app shows wide diagrams at no less than 75% size, and scrolls

### Visual Level

```mermaid
flowchart TD
    A[Diagram drawn] --> B{Natural width fits the pane?}
    B -- yes --> C[Show at natural size]
    B -- no --> D{Fit would go below 75%?}
    D -- no --> E[Fit to the pane]
    D -- yes --> F[Show at 75%, scroll sideways]
    F --> G[Expand button opens a full-size view with zoom]
    classDef changed fill:#bfdbfe,stroke:#1d4ed8,color:#000;
    class D,E,F,G changed;
```

Blue steps change the existing display.

**Context.** `MermaidDiagram` applies `max-width: 100%` to the SVG. A 2000 px diagram is squeezed to about 35%.

**Choice.** Display width is `max(min(natural, pane), 0.75 × natural)`. Wider diagrams scroll sideways inside their box. An Expand button opens a modal at natural size with zoom in, zoom out, and fit. The same component is used in Browse, the capture preview, and the queue, so all three change.

**Consequences.** Some diagrams need a sideways scroll. Expand covers the rest.

**Given up.** The "everything visible at once" look for wide diagrams. The 75% floor is a starting value to tune by eye.

## ADR 6 — An Obsidian CSS snippet lets wide diagrams scroll

### Visual Level

```mermaid
flowchart TD
    A[Add snippet file] --> B[Enable it in appearance.json]
    B --> C[Wide diagrams scroll in Obsidian]
    C --> D[Turn off in Obsidian settings to undo]
    classDef new fill:#fde68a,stroke:#b45309,color:#000;
    class A,B,C,D new;
```

Yellow steps are all new.

**Context.** Even after the fit, about 34 diagrams stay below 75% size in every layout I tried. Obsidian shrinks them like the app did.

**Choice.** Add `.obsidian/snippets/wide-mermaid.css` and list it in `enabledCssSnippets` in `.obsidian/appearance.json`. The snippet removes the `max-width` on diagram SVGs and lets the diagram box scroll sideways.

**Consequences.** Reversible: turn the snippet off in Obsidian, or delete the file.

**Given up.** It changes your Obsidian settings. **I cannot run Obsidian here, so I cannot test it.** The CSS selectors match Obsidian's published structure and may differ in your version. Treat the snippet as unverified until you look at a wide diagram in Obsidian.

## Open questions

1. **Hand-written diagrams.** The 165 pages include notes you may have written in Obsidian. Should the rewrite touch only pages the app wrote (those with `source_type`, `concept_page`, or a `sources/` link in frontmatter), or all of them? I recommend all, because `--dry-run` lists every page first, and the backup makes it reversible.
2. **Pasted diagrams.** Today a diagram inside a pasted clip is kept exactly as pasted. With this change it is fitted. You see the fitted version in the preview and can edit it. Is that acceptable, or should pasted diagrams stay untouched unless you press a "Fit diagram" button?
3. **Intake points.** `_normalize_mermaid` is called at 3 places (`main.py` lines 588, 636, 1402). The pasted-clip path does not call it. `wiki_writer.py` also writes diagrams at lines 406 and 683. I will confirm during build that every write goes through `fit()`.
4. **Obsidian snippet.** Unverified in Obsidian (ADR 6). If it does not work in your version, I need a screenshot of a wide diagram in Obsidian to adjust the selectors.
5. **Thresholds.** The 4-node minimum, the 28-character wrap trigger, and the 75% display floor are starting values. I will check them against `measure_diagrams.py` after the build.
6. **Mind maps and sequence diagrams** (9 in the vault) are only syntax-repaired. They rendered at acceptable widths in the measurement.
