import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fit_vault_diagrams as fv

LR5 = ('flowchart LR\n    A["Start"] --> B["Second step"]\n    B --> C["Third"]\n'
       '    C --> D["Fourth"]\n    D --> E["Done"]\n')


def _write(root: Path, rel: str, body: str) -> Path:
    p = root / "_wiki" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


@pytest.fixture
def vault(isolated_vault):
    page = _write(isolated_vault, "cs/a.md", f"# A\n\ntext before\n\n```mermaid\n{LR5}```\n\ntext after\n")
    plain = _write(isolated_vault, "cs/b.md", "# B\n\nno diagram here\n")
    raw = _write(isolated_vault, "inbox/processed/c.md", f"```mermaid\n{LR5}```\n")
    return isolated_vault, page, plain, raw


def _bytes(*paths):
    return [p.read_bytes() for p in paths]


def test_dry_run_changes_nothing(vault, capsys):
    root, page, plain, raw = vault
    before = _bytes(page, plain, raw)
    sys.argv = ["x"]
    assert fv.main() == 0
    assert _bytes(page, plain, raw) == before
    assert not fv.backup_root().exists()
    assert "1 pages would change" in capsys.readouterr().out


def test_apply_backs_up_first_and_only_touches_mermaid_text(vault):
    root, page, plain, raw = vault
    old = page.read_text()
    before_other = _bytes(plain, raw)
    sys.argv = ["x", "--apply"]
    assert fv.main() == 0
    new = page.read_text()
    assert "flowchart TD" in new and "text before" in new and "text after" in new
    assert new.replace("flowchart TD", "flowchart LR") == old
    stamp = next(fv.backup_root().iterdir())
    assert (stamp / "cs" / "a.md").read_text() == old
    assert _bytes(plain, raw) == before_other          # untouched pages and raw clips
    assert not (stamp / "cs" / "b.md").exists()         # unchanged pages are not copied


def test_restore_returns_every_page_byte_identical(vault):
    root, page, *_ = vault
    old = page.read_bytes()
    sys.argv = ["x", "--apply"]
    fv.main()
    stamp = next(fv.backup_root().iterdir()).name
    assert page.read_bytes() != old
    sys.argv = ["x", "--restore", stamp]
    assert fv.main() == 0
    assert page.read_bytes() == old


def test_restore_unknown_stamp_exits_1(vault):
    sys.argv = ["x", "--restore", "nope"]
    assert fv.main() == 1


def test_unwritable_backup_folder_stops_before_any_change(vault, monkeypatch):
    root, page, plain, raw = vault
    before = _bytes(page)
    real_mkdir = Path.mkdir

    def deny(self, *a, **k):
        if "diagram-backup" in str(self):
            raise PermissionError("read-only")
        return real_mkdir(self, *a, **k)

    monkeypatch.setattr(Path, "mkdir", deny)
    sys.argv = ["x", "--apply"]
    with pytest.raises(SystemExit) as e:
        fv.main()
    assert "No page was changed" in str(e.value)
    assert _bytes(page) == before


def test_revert_block_restores_only_that_diagram(isolated_vault):
    import measure_diagrams as md
    two = f"# T\n\n```mermaid\n{LR5}```\n\nmiddle\n\n```mermaid\n{LR5}```\n"
    page = _write(isolated_vault, "cs/t.md", two)
    sys.argv = ["x", "--apply"]
    fv.main()
    stamp = next(fv.backup_root().iterdir())
    fitted = page.read_text()
    assert fitted.count("flowchart TD") == 2
    md.revert_block(fv._wiki(), stamp, "cs/t.md|1")
    after = page.read_text()
    assert after.count("flowchart TD") == 1 and after.count("flowchart LR") == 1
    assert "middle" in after
    assert after.index("flowchart TD") < after.index("flowchart LR")   # first block still fitted


def test_mark_wide_only_touches_wide_flowcharts_and_is_repeatable(isolated_vault, monkeypatch):
    import measure_diagrams as md
    page = _write(isolated_vault, "cs/w.md",
                  f"# W\n\n```mermaid\n{LR5}```\n\n```mermaid\nsequenceDiagram\n  A->>B: hi\n```\n\n```mermaid\n{LR5}```\n")
    widths = {"live|cs/w.md|0": {"w": 1200}, "live|cs/w.md|1": {"w": 1300}, "live|cs/w.md|2": {"w": 400}}
    monkeypatch.setattr(md, "render", lambda items: widths)
    monkeypatch.setattr(md, "blocks_in", lambda root, tag: [])
    assert md.mark_wide(fv._wiki()) == 0
    text = page.read_text()
    assert text.count(md.WIDE_DIRECTIVE) == 1                 # only block 0: wide flowchart
    assert text.index(md.WIDE_DIRECTIVE) < text.index("sequenceDiagram")
    assert any(fv.backup_root().rglob("w.md"))
    md.mark_wide(fv._wiki())                                    # second run adds nothing
    assert page.read_text().count(md.WIDE_DIRECTIVE) == 1
