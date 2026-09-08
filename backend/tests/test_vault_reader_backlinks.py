import identity
import vault_reader


def test_backlinks_build_alias_snapshot_once(monkeypatch, tmp_path):
    wiki = tmp_path / "_wiki"
    cs = wiki / "cs"
    science = wiki / "science"
    humanities = wiki / "humanities"
    for folder in (cs, science, humanities, wiki / "meta"):
        folder.mkdir(parents=True, exist_ok=True)

    (cs / "rag.md").write_text(
        "---\ntitle: RAG\naliases: [retrieval grounding]\n---\n\n## RAG\n",
        encoding="utf-8",
    )
    (science / "retrieval-test.md").write_text(
        "---\ntitle: Retrieval Test\n---\n\nRelated: [[retrieval grounding]] and [[rag]].\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))

    real_alias_map = identity.alias_map
    calls = 0

    def counted_alias_map():
        nonlocal calls
        calls += 1
        return real_alias_map()

    monkeypatch.setattr(identity, "alias_map", counted_alias_map)

    backlinks = vault_reader.build_backlinks_index()

    assert calls == 1
    assert backlinks["rag"] == ["retrieval-test"]
