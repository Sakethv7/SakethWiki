import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


def _write_ledger(vault: Path, sig: str, file_written: str) -> None:
    idx = vault / "_wiki" / "meta" / "processed_clips.jsonl"
    idx.write_text(json.dumps({"clip_signature": sig, "file_written": file_written}) + "\n")


def test_ledger_blocks_while_page_exists(isolated_vault, monkeypatch):
    monkeypatch.setattr(main.queue_manager, "get_all", lambda: [])
    page = isolated_vault / "_wiki" / "cs" / "a.md"
    page.parent.mkdir(parents=True)
    page.write_text("x")
    _write_ledger(isolated_vault, "sig1", "_wiki/cs/a.md")
    assert main._is_clip_processed("sig1")


def test_ledger_ignores_deleted_page(isolated_vault, monkeypatch):
    monkeypatch.setattr(main.queue_manager, "get_all", lambda: [])
    _write_ledger(isolated_vault, "sig1", "_wiki/cs/gone.md")
    assert not main._is_clip_processed("sig1")


def test_ledger_handles_duplicate_suffix(isolated_vault, monkeypatch):
    monkeypatch.setattr(main.queue_manager, "get_all", lambda: [])
    _write_ledger(isolated_vault, "sig1", "_wiki/cs/gone.md [duplicate, not written]")
    assert not main._is_clip_processed("sig1")
