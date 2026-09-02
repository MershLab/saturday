"""Skill ranking: confidence-weighted scoring, pin/bury, and the shortlist
skills_prompt_block builds on top of it."""
from __future__ import annotations

import time

from saturday import skill_stats


def test_untried_skill_is_pure_relevance_at_n_zero():
    row = {"used": 0, "considered": 0, "ema_success": 0.5, "last_used": None, "pin": 0}
    now = time.time()
    # confidence is 0 at n=0, so outcome/attention contribute nothing and the
    # score collapses to exactly the relevance term
    assert skill_stats.score_one(row, relevance=0.7, now=now) == 0.7
    assert skill_stats.score_one(row, relevance=0.0, now=now) == 0.0


def test_confidence_grows_with_real_use_and_shifts_weight_off_relevance():
    untried = {"used": 0, "considered": 0, "ema_success": 0.5, "last_used": None, "pin": 0}
    seasoned = {"used": 20, "considered": 20, "ema_success": 1.0, "last_used": time.time(), "pin": 0}
    now = time.time()
    # same low relevance, but the seasoned skill's real record should win out
    s_untried = skill_stats.score_one(untried, relevance=0.1, now=now)
    s_seasoned = skill_stats.score_one(seasoned, relevance=0.1, now=now)
    assert s_seasoned > s_untried


def test_pin_beats_any_learned_score_and_bury_sinks_it():
    now = time.time()
    weak_pinned = {"used": 0, "considered": 0, "ema_success": 0.5, "last_used": None, "pin": 1}
    strong_unpinned = {"used": 50, "considered": 50, "ema_success": 1.0, "last_used": now, "pin": 0}
    strong_buried = {"used": 50, "considered": 50, "ema_success": 1.0, "last_used": now, "pin": -1}

    pinned_score = skill_stats.score_one(weak_pinned, relevance=0.0, now=now)
    strong_score = skill_stats.score_one(strong_unpinned, relevance=1.0, now=now)
    buried_score = skill_stats.score_one(strong_buried, relevance=1.0, now=now)

    assert pinned_score > strong_score
    assert buried_score < strong_score


def test_record_used_and_considered_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("saturday.skill_stats._db_path", lambda: tmp_path / "skills.db")
    skill_stats.record_considered("fetch-url")
    skill_stats.record_considered("fetch-url")
    skill_stats.record_used("fetch-url")
    row = skill_stats.stats("fetch-url")
    assert row["considered"] == 2
    assert row["used"] == 1
    assert row["last_used"] is not None
    assert row["ema_success"] == 0.5  # neutral: nothing has recorded an outcome


def test_record_outcome_moves_the_ema(tmp_path, monkeypatch):
    monkeypatch.setattr("saturday.skill_stats._db_path", lambda: tmp_path / "skills.db")
    skill_stats.record_outcome("fetch-url", True, alpha=0.5)
    assert skill_stats.stats("fetch-url")["ema_success"] == 0.75
    skill_stats.record_outcome("fetch-url", False, alpha=0.5)
    assert skill_stats.stats("fetch-url")["ema_success"] == 0.375


def test_set_pin_roundtrip_and_clamping(tmp_path, monkeypatch):
    monkeypatch.setattr("saturday.skill_stats._db_path", lambda: tmp_path / "skills.db")
    skill_stats.set_pin("deploy", 1)
    assert skill_stats.stats("deploy")["pin"] == 1
    skill_stats.set_pin("deploy", -5)  # clamps to -1, not a bigger negative
    assert skill_stats.stats("deploy")["pin"] == -1
    skill_stats.set_pin("deploy", 0)
    assert skill_stats.stats("deploy")["pin"] == 0


def test_rank_orders_pinned_first_buried_last_and_labels_reasons(tmp_path, monkeypatch):
    monkeypatch.setattr("saturday.skill_stats._db_path", lambda: tmp_path / "skills.db")
    entries = [
        ("youtube-transcript", "pull a transcript from a youtube video"),
        ("fetch-url", "fetch a url and return its text"),
        ("web-scrape", "scrape a web page"),
        ("old-noisy", "an old skill nobody uses"),
    ]
    for _ in range(12):
        skill_stats.record_used("youtube-transcript")
    skill_stats.record_outcome("youtube-transcript", True)
    skill_stats.set_pin("fetch-url", -1)  # buried

    ranked = skill_stats.rank(entries, task_text="")
    names = [r["name"] for r in ranked]
    assert names[0] == "youtube-transcript"
    assert names[-1] == "fetch-url"  # buried sinks to the bottom
    assert "fetch-url" in names  # but is never dropped

    youtube = next(r for r in ranked if r["name"] == "youtube-transcript")
    assert "used 12x" in youtube["reason"]
    untried = next(r for r in ranked if r["name"] == "web-scrape")
    assert untried["untried"] is True
    assert "untried" in untried["reason"]


def test_rank_flags_a_skill_considered_often_but_never_loaded(tmp_path, monkeypatch):
    """The description is wrong, not the skill - this is the one diagnostic
    the used/considered split exists to make visible."""
    monkeypatch.setattr("saturday.skill_stats._db_path", lambda: tmp_path / "skills.db")
    for _ in range(6):
        skill_stats.record_considered("mystery-skill")

    ranked = skill_stats.rank([("mystery-skill", "does something")], task_text="")
    reason = ranked[0]["reason"]
    assert "considered 6x, never loaded" in reason


def test_rank_relevance_uses_real_text_match(tmp_path, monkeypatch):
    monkeypatch.setattr("saturday.skill_stats._db_path", lambda: tmp_path / "skills.db")
    entries = [
        ("deploy-vllm", "spin up a vllm inference server on a gpu box"),
        ("write-poetry", "compose a sonnet about the changing seasons"),
    ]
    ranked = skill_stats.rank(entries, task_text="please spin up a vllm server on the gpu box")
    assert ranked[0]["name"] == "deploy-vllm"
