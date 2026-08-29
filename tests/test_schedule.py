"""Cron parity: 5-field matcher, ScheduleStore persistence, due/mark logic."""
from __future__ import annotations

from datetime import datetime

import pytest

from saturday.schedule import ScheduleStore, _valid_expr, cron_matches


def test_cron_matches_basic_fields():
    dt = datetime(2026, 8, 27, 9, 30)
    assert cron_matches("30 9 * * *", dt) is True
    assert cron_matches("31 9 * * *", dt) is False
    assert cron_matches("30 8 * * *", dt) is False
    assert cron_matches("*/15 9 * * *", dt) is True  # minute 30 is a /15 match
    assert cron_matches("*/20 9 * * *", dt) is False  # 30 is not a /20 match
    assert cron_matches("0,30 9 * * *", dt) is True
    assert cron_matches("* * * * 4", dt) is True  # 2026-08-27 is a Thursday (isoweekday 4)
    assert cron_matches("* * * * 3", dt) is False


def test_cron_dom_dow_or_semantics():
    dt = datetime(2026, 8, 27, 9, 0)  # Thu, 27th
    # both restricted: EITHER match satisfies (standard cron contract)
    assert cron_matches("0 9 27 * 0", dt) is True  # dom matches, dow doesn't
    assert cron_matches("0 9 28 * 4", dt) is True  # dow matches, dom doesn't
    assert cron_matches("0 9 28 * 0", dt) is False
    # one side *: AND semantics
    assert cron_matches("0 9 * * 4", dt) is True
    assert cron_matches("0 9 27 * *", dt) is True


def test_invalid_expressions_rejected():
    assert _valid_expr("0 9 * * *") is True
    assert _valid_expr("60 9 * * *") is False
    assert _valid_expr("0 24 * * *") is False
    assert _valid_expr("0 9 * * 8") is False
    assert _valid_expr("0 9 * *") is False
    assert _valid_expr("junk") is False


def test_store_add_list_remove_and_due(tmp_path):
    store = ScheduleStore(tmp_path / "sched.json")
    s = store.add("morning", "0 9 * * 1-5", "standup notes")
    assert s.id == "morning" and store.list()[0].task == "standup notes"
    with pytest.raises(ValueError):
        store.add("bad", "99 9 * * *", "nope")

    dt = datetime(2026, 8, 27, 9, 0)  # Thu
    due = store.due(now=dt)
    assert [d.id for d in due] == ["morning"]
    store.mark_fired("morning", now=dt)
    assert store.due(now=dt) == [], "must not re-fire the same minute"
    assert [d.id for d in store.due(now=datetime(2026, 8, 28, 9, 0))] == ["morning"], "next weekday fires again"
    # 10:00 same morning: not due
    assert store.due(now=datetime(2026, 8, 27, 10, 0)) == []

    store2 = ScheduleStore(tmp_path / "sched.json")  # persistence round-trip
    assert store2.list()[0].last_fired_minute == "202608270900"
    assert store.remove("morning") is True
    assert store.remove("morning") is False
