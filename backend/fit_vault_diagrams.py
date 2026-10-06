"""
Fit every Mermaid diagram in the vault (see docs/diagram-fit/).

  python backend/fit_vault_diagrams.py                    # dry run: writes nothing
  python backend/fit_vault_diagrams.py --apply            # back up each page, then rewrite
  python backend/fit_vault_diagrams.py --restore <stamp>  # put the backed-up pages back

Only text inside ```mermaid fences changes. Raw captured clips (_wiki/inbox) and
generated reports (_wiki/meta) are left alone.
"""
import argparse
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import diagram_fit

FENCE = re.compile(r"(```mermaid[ \t]*\n)(.*?)(```)", re.S)
SKIP_TOP = {"inbox", "meta"}


def _wiki() -> Path:
    return Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault")) / "_wiki"


def backup_root() -> Path:
    return _wiki() / "meta" / "diagram-backup"


def fit_text(text: str) -> str:
    return FENCE.sub(lambda m: m.group(1) + diagram_fit.fit(m.group(2)) + m.group(3), text)


def pages() -> list[Path]:
    wiki = _wiki()
    return sorted(p for p in wiki.rglob("*.md") if p.relative_to(wiki).parts[0] not in SKIP_TOP)


def plan() -> list[tuple[Path, str, str]]:
    """(path, old text, new text) for every page the fit would change."""
    out = []
    for path in pages():
        old = path.read_text(encoding="utf-8")
        if "```mermaid" not in old:
            continue
        new = fit_text(old)
        if new != old:
            out.append((path, old, new))
    return out


def _direction(text: str) -> str:
    return ",".join(sorted({m.group(1).upper() for m in re.finditer(r"^\s*(?:flowchart|graph)\s+(LR|RL|TD|TB|BT)\b", text, re.M)}))


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def apply(changes: list[tuple[Path, str, str]], stamp: str) -> list[str]:
    """Back up then rewrite. Returns pages that failed (restored from backup)."""
    wiki = _wiki()
    dest = backup_root() / stamp
    try:
        dest.mkdir(parents=True, exist_ok=False)
        probe = dest / ".write-test"
        probe.write_text("ok")
        probe.unlink()
    except OSError as e:
        raise SystemExit(f"Cannot write the backup folder {dest}: {e}. No page was changed.")
    failed = []
    for path, old, new in changes:
        rel = path.relative_to(wiki)
        target = dest / rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            _atomic_write(path, new)
        except OSError as e:
            shutil.copy2(target, path) if target.exists() else None
            failed.append(f"{rel}: {e}")
    return failed


def restore(stamp: str) -> int:
    src = backup_root() / stamp
    if not src.is_dir():
        print(f"No backup named {stamp} in {backup_root()}")
        return 1
    wiki = _wiki()
    n = 0
    for f in sorted(src.rglob("*.md")):
        shutil.copy2(f, wiki / f.relative_to(src))
        n += 1
    print(f"Restored {n} pages from {src}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--restore", metavar="STAMP")
    args = ap.parse_args()
    if args.restore:
        return restore(args.restore)
    changes = plan()
    wiki = _wiki()
    for path, old, new in changes:
        blocks = sum(1 for a, b in zip(FENCE.findall(old), FENCE.findall(new)) if a[1] != b[1])
        print(f"{path.relative_to(wiki)}  blocks changed: {blocks}  direction: {_direction(old) or '-'} -> {_direction(new) or '-'}")
    print(f"\n{len(changes)} pages would change.")
    if not args.apply:
        print("Dry run: nothing was written. Use --apply to back up and rewrite.")
        return 0
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    failed = apply(changes, stamp)
    for line in failed:
        print("FAILED (restored):", line)
    print(f"Rewrote {len(changes) - len(failed)} pages. Backup: {backup_root() / stamp}")
    print(f"Undo with: python backend/fit_vault_diagrams.py --restore {stamp}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
