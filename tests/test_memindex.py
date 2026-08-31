"""Memory index over MEMORY.md: parsing, salience, A-MEM back-linking, search."""
from __future__ import annotations

import time

import pytest

from saturday.memindex import MemoryIndex, parse_notes


def _idx(tmp_path):
    return MemoryIndex(db_path=tmp_path / "m.db")


def test_a_leading_link_names_a_note_and_an_inline_one_only_points():
    """Conflating them made 'see [[jwt-keys]]' claim that slug and push the
    note actually defining it aside."""
    notes = parse_notes(
        "- The auth service signs JWTs, see [[jwt-keys]]\n"
        "- [[jwt-keys]] Keys live in the vault\n"
    )
    assert [n["slug"] for n in notes] == [
        "the-auth-service-signs-jwts-see-jwt-keys", "jwt-keys"]
    assert notes[0]["refs"] == ["jwt-keys"]
    assert notes[1]["refs"] == []
    assert notes[1]["text"] == "Keys live in the vault"


def test_headings_and_blanks_are_structure_not_notes():
    notes = parse_notes("# Memory\n\n## Section\n\n- a real note here\n\nanother real note\n")
    assert [n["text"] for n in notes] == ["a real note here", "another real note"]
    assert parse_notes("") == []
    assert parse_notes("# only a heading") == []


def test_duplicate_slugs_stay_distinct():
    notes = parse_notes("- same opening words here\n- same opening words here\n")
    assert len({n["slug"] for n in notes}) == 2


def test_salience_and_created_at_survive_a_reparse(tmp_path):
    """Salience records what was true when a note first appeared; recomputing
    it on every reparse would erase the history it exists to carry."""
    idx = _idx(tmp_path)
    md = "- alpha note about kubernetes ingress\n"
    idx.reindex(md)
    conn = idx._connect()
    before = conn.execute("SELECT salience, created_at FROM memory_nodes").fetchone()

    idx.reindex(md + "- alpha note about kubernetes ingress rules and paths\n")
    after = conn.execute(
        "SELECT salience, created_at FROM memory_nodes WHERE slug=?",
        ("alpha-note-about-kubernetes-ingress",)).fetchone()
    assert after == before
    idx.close()


def test_amem_backlinks_land_on_both_notes(tmp_path):
    """Without this the graph is append-only: an old note could never gain a
    link that a later note revealed."""
    idx = _idx(tmp_path)
    idx.reindex("- Postgres connection pool is set to 20\n")
    idx.reindex("- Postgres connection pool is set to 20\n"
                "- We raised the postgres connection pool from 20 to 50\n")
    g = idx.graph()
    assert len(g["nodes"]) == 2
    pairs = {(e["from"], e["to"]) for e in g["edges"]}
    a, b = g["nodes"][0]["id"], g["nodes"][1]["id"]
    assert (a, b) in pairs and (b, a) in pairs, "the edge must land on both"
    idx.close()


def test_a_correction_is_a_contradiction_not_a_relation(tmp_path):
    idx = _idx(tmp_path)
    idx.reindex("- The deploy pipeline used helm charts for everything\n")
    idx.reindex("- The deploy pipeline used helm charts for everything\n"
                "- We no longer use helm for the deploy pipeline; argocd replaced it\n")
    rels = {e["relation"] for e in idx.graph()["edges"]}
    assert rels == {"contradicts"}, "keeping relates_to too asserts two things about one pair"
    idx.close()


def test_search_ranks_and_reaches_one_hop(tmp_path):
    idx = _idx(tmp_path)
    idx.reindex("- Postgres connection pool is set to 20\n")
    idx.reindex("- Postgres connection pool is set to 20\n"
                "- We raised the postgres connection pool from 20 to 50\n"
                "- Unrelated note about frontend bundling\n")
    hits = idx.search("postgres pool")
    assert hits and hits == sorted(hits, key=lambda h: -h["score"])
    assert all(0.0 <= h["score"] <= 1.0 for h in hits)
    assert any(h["matched"] for h in hits)
    texts = " ".join(h["text"] for h in hits)
    assert "frontend" not in texts
    idx.close()


def test_search_scores_differ_rather_than_all_collapsing(tmp_path):
    """An earlier version read a leaked loop variable, so every result came
    back with the same score."""
    idx = _idx(tmp_path)
    idx.reindex("- kubernetes ingress uses nginx\n"
                "- kubernetes ingress rules live in the chart\n"
                "- kubernetes nodes autoscale on cpu\n")
    hits = idx.search("kubernetes ingress")
    assert len({h["score"] for h in hits}) > 1
    idx.close()


def test_removed_notes_leave_the_index(tmp_path):
    idx = _idx(tmp_path)
    idx.reindex("- first note about caching\n- second note about queues\n")
    assert len(idx.graph()["nodes"]) == 2
    stats = idx.reindex("- first note about caching\n")
    assert stats["removed"] == 1
    assert len(idx.graph()["nodes"]) == 1
    idx.close()


def test_consolidate_archives_but_never_deletes(tmp_path):
    idx = _idx(tmp_path)
    idx.reindex("- a note that will go stale about widgets\n")
    conn = idx._connect()
    conn.execute("UPDATE memory_nodes SET salience=0.01, last_touched=?",
                 (time.time() - 400 * 86400,))
    conn.commit()

    dry = idx.consolidate(dry_run=True)
    assert len(dry["archived"]) == 1
    assert conn.execute("SELECT archived FROM memory_nodes").fetchone()[0] == 0

    idx.consolidate()
    assert conn.execute("SELECT archived FROM memory_nodes").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM memory_nodes").fetchone()[0] == 1
    assert idx.search("widgets") == []
    assert idx.search("widgets", include_archived=True)
    idx.close()


def test_scopes_do_not_collide(tmp_path):
    idx = _idx(tmp_path)
    idx.reindex("- shared slug text here\n", scope="global")
    idx.reindex("- shared slug text here\n", scope="project:/tmp/x")
    assert len(idx.graph()["nodes"]) == 2
    assert len(idx.graph(scope="global")["nodes"]) == 1
    idx.close()


def test_memory_graph_fact_layer_uses_the_index_not_raw_lines(tmp_path, monkeypatch):
    """The one view meant to explain memory must show what the index knows -
    salience and contradictions - not a poorer re-parse of the same file."""
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setattr("saturday.config.CONFIG_DIR", cfg)
    monkeypatch.setattr("saturday.config.get_config_dir", lambda: cfg)
    (cfg / "MEMORY.md").write_text(
        "- The deploy pipeline used helm charts for everything\n"
        "- We no longer use helm for the deploy pipeline; argocd replaced it\n"
        "- Nightly backups land in S3 under prod/db\n", encoding="utf-8")

    from saturday.memgraph import build_graph

    g = build_graph(None)
    facts = [n for n in g["nodes"] if n["kind"] == "fact"]
    assert len(facts) == 3
    assert all("salience" in n["meta"] and "slug" in n["meta"] for n in facts)
    kinds = {e["kind"] for e in g["edges"]}
    assert "contradicts" in kinds, "a correction must reach the picture as a contradiction"


def test_memory_graph_still_lists_facts_if_the_index_fails(tmp_path, monkeypatch):
    """The index is scaffolding over a plain file; losing it must not blank
    the notes themselves."""
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setattr("saturday.config.CONFIG_DIR", cfg)
    monkeypatch.setattr("saturday.config.get_config_dir", lambda: cfg)
    (cfg / "MEMORY.md").write_text("- a note that must still show up\n", encoding="utf-8")

    import saturday.memindex as mi

    class Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("index unavailable")

    monkeypatch.setattr(mi, "MemoryIndex", Boom)
    from saturday.memgraph import build_graph

    g = build_graph(None)
    assert [n["label"] for n in g["nodes"] if n["kind"] == "fact"] == \
        ["a note that must still show up"]
