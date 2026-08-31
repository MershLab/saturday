"""Three-factor memory scoring: recency, relevance, salience."""
from __future__ import annotations

import json
import time

import pytest

from saturday.memscore import (
    DEFAULT_HALF_LIFE_DAYS,
    SalienceIndex,
    combine,
    diffuse,
    jaccard,
    minhash,
    normalize_relevance,
    recency,
)


def test_recency_halves_over_the_half_life():
    now = time.time()
    assert recency(now, now) == pytest.approx(1.0)
    assert recency(now - DEFAULT_HALF_LIFE_DAYS * 86400, now) == pytest.approx(1 / 2.718, abs=0.01)
    assert recency(now - 3650 * 86400, now) < 0.001
    assert recency(0, now) == 0.0          # unknown timestamp scores nothing
    assert recency(now + 500, now) == pytest.approx(1.0)   # clock skew: not > 1


def test_salience_is_novelty_against_what_is_already_indexed():
    si = SalienceIndex()
    assert si.add("the auth module signs JWTs with RS256") == pytest.approx(1.0)
    assert si.add("the auth module signs JWTs with RS256") == pytest.approx(0.0, abs=0.01)
    assert si.add("the auth module signs JWTs with RS256!") < 0.15
    assert si.add("billing runs a monthly stripe reconciliation") > 0.9


def test_minhash_is_deterministic_across_processes():
    """hash() is randomly seeded per process; signatures must not be."""
    a = minhash("stable input text for hashing")
    b = minhash("stable input text for hashing")
    assert a == b
    assert jaccard(a, b) == 1.0
    assert jaccard(a, minhash("something entirely different here")) < 0.2


def test_diffusion_reaches_one_hop_and_stops():
    out = diffuse({"a": 1.0}, [("a", "b"), ("b", "c")], damping=0.5)
    assert out["b"] == 0.5
    assert out.get("c", 0.0) == 0.0, "two hops would make everything relevant"


def test_weights_sum_to_one_and_relevance_dominates():
    assert combine(1, 1, 1) == pytest.approx(1.0)
    assert combine(0, 1, 0) > combine(1, 0, 0)
    assert combine(0, 1, 0) > combine(0, 0, 1)


def test_normalize_relevance_handles_degenerate_sizes():
    assert normalize_relevance([]) == []
    assert normalize_relevance([0.0]) == [1.0]
    assert normalize_relevance([0.0] * 3) == [1.0, 0.5, 0.0]


def _write(root, name, ts, msgs):
    (root / f"{name}.jsonl").write_text(
        "\n".join(json.dumps({"ts": ts, "type": "messages",
                              "messages": [{"role": "user", "content": m}]}) for m in msgs) + "\n",
        encoding="utf-8")


def test_recall_prefers_the_recent_novel_hit_over_stale_duplicates(tmp_path):
    """The behaviour change: BM25 alone put four identical stale rows first."""
    from saturday.recall import RecallIndex

    now = time.time()
    _write(tmp_path, "old", now - 200 * 86400, ["the auth service uses helm charts"] * 4)
    _write(tmp_path, "recent", now - 86400, ["the auth service uses argocd now, helm was dropped"])
    idx = RecallIndex(store_root=tmp_path, db_path=tmp_path / "r.db")
    idx.rebuild()

    hits = idx.search("auth service helm", k=6)
    assert hits, "the query must still match"
    sessions = [h["session"] for h in hits]
    assert sessions.index("recent") < sessions.index("old", 1), \
        "a recent, novel hit must outrank redundant stale copies"
    assert all("score" in h for h in hits)
    assert hits == sorted(hits, key=lambda h: -h["score"])
    idx.close()


def test_recall_index_survives_a_pre_salience_database(tmp_path):
    """An index built before the column existed must not break on open."""
    import sqlite3

    db = tmp_path / "r.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE recall_meta(k TEXT PRIMARY KEY, v TEXT);"
        "CREATE TABLE recall_rows(id INTEGER PRIMARY KEY, session TEXT, ts REAL,"
        " role TEXT, text TEXT);"
    )
    con.commit()
    con.close()

    from saturday.recall import RecallIndex

    _write(tmp_path, "s", time.time(), ["kubernetes deployment notes"])
    idx = RecallIndex(store_root=tmp_path, db_path=db)
    idx.rebuild()
    assert idx.search("kubernetes", k=3)
    idx.close()
