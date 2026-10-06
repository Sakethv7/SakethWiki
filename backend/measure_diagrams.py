"""
Measure how the vault's Mermaid diagrams really render (see docs/diagram-fit/).
Uses headless Chrome and the Mermaid library the app ships (frontend/node_modules).

  python backend/measure_diagrams.py                      # measure the live vault
  python backend/measure_diagrams.py --pair <stamp>       # backup vs live: list diagrams that broke or got wider
  python backend/measure_diagrams.py --pair <stamp> --revert   # also put those diagrams back to their backed-up text
  python backend/measure_diagrams.py --mark-wide          # give diagrams still wider than the pane a fixed width (backed up)

Not part of the running app. Needs Google Chrome.
"""
import argparse
import html
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MERMAID_JS = ROOT / "frontend" / "node_modules" / "mermaid" / "dist" / "mermaid.min.js"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PANE = 700  # px: about the width of the app's content area and of an Obsidian note
FENCE = re.compile(r"```mermaid[ \t]*\n(.*?)```", re.S)

PAGE = """<!doctype html><meta charset=utf-8><pre id=out>running</pre>
<script src="file://%(mermaid)s"></script><script src="items.js"></script>
<script>
mermaid.initialize({startOnLoad:false, securityLevel:"antiscript", theme:"base"});
(async()=>{ const res={};
  for (const it of ITEMS){ try {
      const {svg}=await mermaid.render("d"+Math.random().toString(36).slice(2), it.src);
      const v=svg.match(/viewBox="([\\d.\\-\\s]+)"/)[1].split(/\\s+/).map(Number);
      res[it.key]={w:Math.round(v[2]),h:Math.round(v[3])};
    } catch(e){ res[it.key]={err:String(e.message||e).split("\\n").slice(0,2).join(" | ").slice(0,200)}; } }
  document.getElementById("out").textContent="RESULT:"+JSON.stringify(res);
})();
</script>"""


def blocks_in(root: Path, tag: str) -> list[dict]:
    items = []
    for md in sorted(root.rglob("*.md")):
        rel = md.relative_to(root)
        if rel.parts[0] in ("inbox", "meta"):
            continue
        for i, m in enumerate(FENCE.finditer(md.read_text(encoding="utf-8"))):
            items.append({"key": f"{tag}|{rel}|{i}", "src": m.group(1)})
    return items


def render(items: list[dict]) -> dict:
    if not Path(CHROME).exists():
        raise SystemExit("Google Chrome not found. Install it or edit CHROME in this file.")
    if not MERMAID_JS.exists():
        raise SystemExit(f"Run `npm install` in frontend/ first: {MERMAID_JS} is missing.")
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        (t / "items.js").write_text("var ITEMS=" + json.dumps(items) + ";", encoding="utf-8")
        (t / "m.html").write_text(PAGE % {"mermaid": MERMAID_JS}, encoding="utf-8")
        out = subprocess.run(
            [CHROME, "--headless=new", "--disable-gpu", "--allow-file-access-from-files",
             f"--virtual-time-budget={60000 + 1500 * len(items)}", "--dump-dom", f"file://{t / 'm.html'}"],
            capture_output=True, text=True, timeout=60 + 3 * len(items),
        ).stdout
    m = re.search(r"RESULT:(.*?)</pre>", out, re.S)
    if not m:
        raise SystemExit("Chrome produced no result. Try running it once by hand to see the error.")
    return json.loads(html.unescape(m.group(1)))


WIDE_DIRECTIVE = '%%{init: {"flowchart": {"useMaxWidth": false}}}%%'


def mark_wide(wiki: Path) -> int:
    """Obsidian draws a diagram at the pane width (Mermaid writes width="100%"). With useMaxWidth:false
    it draws at its real pixel width, and the snippet in .obsidian/snippets lets it scroll."""
    from datetime import datetime

    import fit_vault_diagrams as fv

    res = render(blocks_in(wiki, "live"))
    wide: dict[str, set[int]] = {}
    for key, v in res.items():
        _, rel, idx = key.split("|")
        if v.get("w", 0) > PANE:
            wide.setdefault(rel, set()).add(int(idx))
    changes = []
    for rel, idxs in wide.items():
        old = (wiki / rel).read_text(encoding="utf-8")
        counter = iter(range(10_000))

        def edit(m):
            i = next(counter)
            src = m.group(1)
            first = next((ln for ln in src.split("\n") if ln.strip() and not ln.strip().startswith("%%")), "")
            if i not in idxs or "useMaxWidth" in src or not re.match(r"\s*(flowchart|graph)\b", first):
                return m.group(0)
            return m.group(0).replace(src, WIDE_DIRECTIVE + "\n" + src, 1)

        new = FENCE.sub(edit, old)
        if new != old:
            changes.append((wiki / rel, old, new))
    print(f"{len(changes)} pages have a flowchart wider than {PANE}px.")
    if not changes:
        return 0
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    failed = fv.apply(changes, stamp)
    for line in failed:
        print("FAILED (restored):", line)
    print(f"Marked {len(changes) - len(failed)} pages. Backup: {fv.backup_root() / stamp}")
    print(f"Undo with: python backend/fit_vault_diagrams.py --restore {stamp}")
    return 1 if failed else 0


def revert_block(wiki: Path, back: Path, key: str) -> None:
    """Put one diagram (key = 'path|index') back to its backed-up text. Other text on the page is untouched."""
    rel, idx = key.rsplit("|", 1)
    idx = int(idx)
    old_block = list(FENCE.finditer((back / rel).read_text(encoding="utf-8")))[idx].group(1)
    live = (wiki / rel).read_text(encoding="utf-8")
    spans = list(FENCE.finditer(live))
    m = spans[idx]
    new = live[:m.start(1)] + old_block + live[m.end(1):]
    tmp = (wiki / rel).with_suffix(".md.tmp")
    tmp.write_text(new, encoding="utf-8")
    tmp.replace(wiki / rel)


def stats(res: dict) -> None:
    ok = {k: v for k, v in res.items() if "w" in v}
    bad = {k: v for k, v in res.items() if "err" in v}
    print(f"diagrams: {len(res)}   rendered: {len(ok)}   failed: {len(bad)}")
    for k, v in bad.items():
        print(f"  FAILED {k.split('|', 1)[1]}: {v['err']}")
    if ok:
        scale = [min(1, PANE / v["w"]) for v in ok.values()]
        print(f"median natural width: {statistics.median(v['w'] for v in ok.values()):.0f} px")
        print(f"shrunk below 75% at {PANE}px: {sum(s < 0.75 for s in scale)}   below 50%: {sum(s < 0.5 for s in scale)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", metavar="STAMP")
    ap.add_argument("--revert", action="store_true", help="with --pair: restore regressed diagrams from the backup")
    ap.add_argument("--mark-wide", action="store_true", help="add useMaxWidth:false to diagrams wider than the pane")
    args = ap.parse_args()
    wiki = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault")) / "_wiki"
    if args.mark_wide:
        return mark_wide(wiki)
    if not args.pair:
        stats(render(blocks_in(wiki, "live")))
        return 0
    back = wiki / "meta" / "diagram-backup" / args.pair
    if not back.is_dir():
        print(f"No backup named {args.pair}")
        return 1
    old_items = blocks_in(back, "old")
    only = {k.split("|", 1)[1] for k in (i["key"] for i in old_items)}
    new_items = [i for i in blocks_in(wiki, "new") if i["key"].split("|", 1)[1] in only]
    res = render(old_items + new_items)
    print("BEFORE (backup):"); stats({k: v for k, v in res.items() if k.startswith("old|")})
    print("AFTER (live):"); stats({k: v for k, v in res.items() if k.startswith("new|")})
    regress = []
    for k in only:
        o, n = res.get("old|" + k, {}), res.get("new|" + k, {})
        if "err" in n and "err" not in o:
            regress.append((k, "now fails to parse"))
        elif "w" in o and "w" in n and n["w"] > o["w"]:
            regress.append((k, f"wider: {o['w']} -> {n['w']} px"))
    print(f"\nregressions: {len(regress)}")
    for k, why in regress:
        print(f"  {k}  {why}")
    if args.revert and regress:
        for k, _ in regress:
            revert_block(wiki, back, k)
        print(f"Reverted {len(regress)} diagrams to their backed-up text.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
