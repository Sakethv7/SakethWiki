"""
Measure capture_compare against labeled pairs.

  python backend/calibrate_compare.py --gate-only   # no LLM: containment scores and the cheap gate
  python backend/calibrate_compare.py               # full comparison (one Haiku call per non-duplicate pair)

Pairs live in docs/capture-conflict-review/calibration_pairs.json. Each pair is
compared against a temp copy of the vault with `hide_pages` removed, so a clip's
own page cannot match itself.
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

PAIRS = Path(__file__).resolve().parents[1] / "docs" / "capture-conflict-review" / "calibration_pairs.json"
BANDS = ["duplicate", "overlap", "conflict", "distinct", "unknown"]


def _temp_vault(real: Path, hide: set[str]) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="calib-vault-"))
    for folder in ("cs", "science"):
        src = real / "_wiki" / folder
        if not src.exists():
            continue
        dst = tmp / "_wiki" / folder
        dst.mkdir(parents=True)
        for md in src.glob("*.md"):
            if md.stem not in hide:
                shutil.copy2(md, dst / md.name)
    (tmp / "_wiki" / "meta").mkdir(parents=True, exist_ok=True)
    return tmp


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gate-only", action="store_true")
    args = ap.parse_args()

    real = Path(os.environ.get("VAULT_PATH", "/Users/sakethv7/SakethVault"))
    import capture_compare as cc

    if args.gate_only:
        def no_llm(**kw):
            raise RuntimeError("gate-only run")
        cc.llm_client.complete = no_llm

    pairs = json.loads(PAIRS.read_text(encoding="utf-8"))["pairs"]
    matrix: Counter = Counter()
    for p in pairs:
        tmp = _temp_vault(real, set(p.get("hide_pages", [])))
        os.environ["VAULT_PATH"] = str(tmp)
        try:
            r = cc.build_report({"title": p["clip_title"], "summary": p["clip_claims"], "suggested_page": p["suggested_page"]})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        got = r["band"]
        matrix[(p["label"], got)] += 1
        flag = "" if got == p["label"] else "  <-- differs"
        print(f"{p['id']} label={p['label']:<9} got={got:<9} containment={r['match_score']:.2f} target={r['target_page']}{flag}")

    print("\nrows = your label, columns = result")
    print(f"{'':<10}" + "".join(f"{b:>10}" for b in BANDS))
    for label in BANDS[:4]:
        print(f"{label:<10}" + "".join(f"{matrix[(label, b)]:>10}" for b in BANDS))
    correct = sum(n for (a, b), n in matrix.items() if a == b)
    print(f"\nagree: {correct}/{len(pairs)}")
    dup_wrong = sum(n for (a, b), n in matrix.items() if b == "duplicate" and a != "duplicate")
    print(f"clips wrongly marked duplicate (the unsafe error): {dup_wrong}")


if __name__ == "__main__":
    main()
