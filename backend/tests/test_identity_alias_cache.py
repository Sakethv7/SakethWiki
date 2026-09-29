import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import identity


def write_page(path: Path, aliases: str) -> None:
    path.write_text(f"---\ntitle: RAG\naliases: [{aliases}]\n---\n\nbody\n", encoding="utf-8")


def test_alias_map_rebuilds_when_a_page_changes(monkeypatch, tmp_path):
    (tmp_path / "_wiki" / "cs").mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    page = tmp_path / "_wiki" / "cs" / "rag-pipeline.md"

    write_page(page, "retrieval pipeline")
    assert identity.resolve_slug("retrieval pipeline") == "rag-pipeline"

    write_page(page, "grounded generation")
    stat = page.stat()
    os.utime(page, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))  # guarantee a new mtime
    assert identity.resolve_slug("grounded generation") == "rag-pipeline"
    assert identity.resolve_slug("retrieval pipeline") == "retrieval-pipeline"


def test_alias_map_returns_a_copy(monkeypatch, tmp_path):
    (tmp_path / "_wiki" / "cs").mkdir(parents=True)
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    identity.alias_map()["poisoned"] = "x"
    assert "poisoned" not in identity.alias_map()
