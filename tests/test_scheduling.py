"""Cron parity: 5-field matcher, ScheduleStore persistence, due/mark logic.
Also: Telegram gateway (session reuse, backoff), local usage accounting."""
from __future__ import annotations
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime
import pytest
from saturday.schedule import ScheduleStore, _valid_expr, cron_matches
TOKEN = "tok"


@pytest.fixture(autouse=True)
def _hermetic_user_config(monkeypatch, tmp_path):
    from saturday import config as cfgmod
    import saturday.mcp_plugin as mcpmod

    monkeypatch.setattr(cfgmod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(mcpmod, "load_mcp_config", lambda *a, **k: {})
    monkeypatch.setattr(cfgmod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfgmod, "save_config", lambda partial: None)


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


def test_cron_dow_7_matches_sunday():
    from saturday.schedule import _valid_expr, cron_matches

    sunday = datetime(2026, 8, 30, 9, 0)
    monday = datetime(2026, 8, 31, 9, 0)
    assert _valid_expr("0 9 * * 7")
    assert cron_matches("* * * * 7", sunday), "dow=7 must match Sunday"
    assert not cron_matches("* * * * 7", monday), "dow=7 must not match Monday"
    assert cron_matches("* * * * 0", sunday), "dow=0 still matches Sunday"
    assert cron_matches("* * * * 0,7", sunday), "combined 0,7 matches Sunday once"


def test_cron_dow_7_end_to_end(tmp_path):
    from saturday.schedule import ScheduleStore

    store = ScheduleStore(path=tmp_path / "schedules.json")
    store.add("sun", "0 9 * * 7", "weekly sunday task")
    due = store.due(now=datetime(2026, 8, 30, 9, 0))
    assert [s.id for s in due] == ["sun"]


# --- helpers pulled from tests/test_v05_platform.py ---
class FakeTransport:
    def __init__(self, updates: list[dict]):
        self.updates = list(updates)
        self.sent: list[tuple] = []

    def get_updates(self):
        out = self.updates
        self.updates = []
        return out

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


def scripted_agent_factory():
    from saturday.types import Trajectory

    class A:
        memory = None
        cfg = None

        def run(self, task, **kw):
            return Trajectory(task=task, system_prompt="s", final_answer=f"echo:{task[:60]}", stop_reason="done")

    return A()


def transport_sent_last_text(gw):
    return gw.transport.sent[-1][1]


def _wait_for(cond, message, timeout=5.0):
    """Wait for a gateway worker thread to land its reply.

    The gateway answers on a daemon thread by design - the poll loop must keep
    polling while a chat works - so every assertion about transport.sent is a
    race unless it waits."""
    import time as _t

    deadline = _t.monotonic() + timeout
    while _t.monotonic() < deadline:
        if cond():
            return
        _t.sleep(0.01)
    raise AssertionError(message)


def test_telegram_gateway_end_to_end():
    from saturday.gateway import TelegramGateway

    updates = [
        {"update_id": 1, "message": {"chat": {"id": 42}, "text": "hello bot"}},
        {"update_id": 2, "message": {"chat": {"id": 43}, "text": "intruder"}},
    ]
    transport = FakeTransport(updates)
    gw = TelegramGateway("tok", scripted_agent_factory, allowed_chat_ids={42}, transport=transport)

    handled = gw.poll_once()
    assert handled == 1
    # handle_update dispatches to a daemon thread and returns, so the reply is
    # not on the transport yet. Reading transport.sent straight after
    # poll_once() only passed because the rest of the suite happened to give
    # that thread time; running this file alone it failed outright.
    _wait_for(lambda: any(t[0] == 42 for t in transport.sent), "no reply to the allowed chat")
    _wait_for(lambda: any(t[0] == 43 for t in transport.sent), "no liveness reply to the stranger")
    sent = [t for t in transport.sent if t[0] == 42]
    blocked = [t for t in transport.sent if t[0] == 43]
    assert sent and sent[0][1] == "echo:hello bot"
    # r2: one liveness reply per stranger chat, then silent drop (probe oracle)
    assert blocked and blocked[0][1] == "Not authorized for this bot."

    transport.updates = [
        {"update_id": 3, "message": {"chat": {"id": 43}, "text": "intruder again"}},
    ]
    handled2 = gw.poll_once()
    assert handled2 == 0
    assert len([t for t in transport.sent if t[0] == 43]) == 1, "must not reply to repeat probes"


def test_gateway_session_reuse_and_error_path():
    from saturday.gateway import TelegramGateway

    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        return scripted_agent_factory()

    class Boom:
        def run(self, task, **kw):
            raise RuntimeError("model exploded")

    transport = FakeTransport([{"update_id": 5, "message": {"chat": {"id": 7}, "text": "a"}}])
    gw = TelegramGateway("t", factory, transport=transport)
    s1 = gw.session_for(7)
    s2 = gw.session_for(7)
    assert s1 is s2

    boom_gw = TelegramGateway(
        "t",
        lambda: Boom(),
        transport=FakeTransport([{"update_id": 6, "message": {"chat": {"id": 8}, "text": "x"}}]),
    )
    boom_gw.poll_once()
    assert "agent error" in transport_sent_last_text(boom_gw)


def test_gateway_backoff_on_transport_failure():
    from saturday.gateway import TelegramGateway

    class FlakyTransport:
        def __init__(self):
            self.calls = 0

        def get_updates(self):
            self.calls += 1
            if self.calls <= 2:
                raise ConnectionError("telegram down")
            return []

        def send_message(self, chat_id, text):
            pass

    sleeps = []
    gw = TelegramGateway("t", lambda: None, transport=FlakyTransport())
    gw._tick(sleeps.append)
    gw._tick(sleeps.append)
    ok = gw._tick(sleeps.append)

    assert sleeps[:2] == [2.0, 4.0], f"backoff not exponential: {sleeps}"
    assert ok is True
    assert gw.consecutive_failures == 0


# --- helpers pulled from tests/test_usage.py ---
class _Server:
    def __init__(self, app):
        from saturday.webui import AppServer

        self.http = AppServer(("127.0.0.1", 0), app, token=TOKEN)
        self.base = f"http://127.0.0.1:{self.http.server_address[1]}"
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.http.shutdown()
        self.http.server_close()


def _req(base, path, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(base + path, data=data, method=method)
    r.add_header("X-Saturday-Token", TOKEN)
    if data:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def test_record_and_summary(tmp_path):
    from saturday.usage import load_entries, record_usage, usage_summary

    assert load_entries() == []
    record_usage(provider="openai", model="m1", session="s", steps=3, total_tokens=100, stop_reason="done")
    record_usage(provider="openrouter", model="m2", session="s2", steps=1, total_tokens=50, stop_reason="done")
    entries = load_entries()
    assert len(entries) == 2 and entries[0]["model"] == "m1"
    summary = usage_summary()
    assert summary["turns"] == 2
    assert summary["total_tokens"] == 150
    models = {m["model"]: m["tokens"] for m in summary["models"]}
    assert models["openai/m1"] == 100
    assert len(summary["days"]) == 1


def test_old_entries_ignored_not_deleted(tmp_path):
    from saturday.usage import DAYS_SHOWN, load_entries, record_usage, usage_summary

    record_usage(provider="p", model="m", total_tokens=10)
    # backdate a line beyond the window by rewriting the file
    p = tmp_path / "usage.jsonl"
    lines = p.read_text(encoding="utf-8").splitlines()
    import time as t

    old = json.loads(lines[0])
    old["ts"] = t.time() - (DAYS_SHOWN + 5) * 86_400
    old["day"] = "2000-01-01"
    p.write_text(json.dumps(old) + "\n" + "\n".join(lines) + "\n", encoding="utf-8")
    entries = load_entries()
    assert all(e["day"] != "2000-01-01" for e in entries)
    assert usage_summary()["turns"] == len(entries)


def test_corrupt_lines_skipped(tmp_path):
    from saturday.usage import load_entries

    p = tmp_path / "usage.jsonl"
    p.write_text("{not json}\n\n{\"ts\": 1}\n", encoding="utf-8")  # ts=1 -> ancient, dropped
    assert load_entries() == []


def test_chat_turn_records_usage_and_state_exposes(tmp_path):
    from fakes import make_scripted_model
    from saturday.projects import ProjectStore
    from saturday.webui import AppState

    app = AppState(
        store_root=tmp_path / "sessions",
        projects_store=ProjectStore(tmp_path / "projects.json"),
        cfg_overrides={"safety_mode": "off", "workspace_root": str(tmp_path / "ws")},
    )
    fake = make_scripted_model([{"content": "answer!"}])
    orig = app._new_agent

    def patched(cfg):
        agent = orig(cfg)
        agent._ensure_client = lambda: fake
        return agent

    app._new_agent = patched
    with _Server(app) as srv:
        payload = {"text": "hi", "session_id": ""}
        r = urllib.request.Request(srv.base + "/api/chat", data=json.dumps(payload).encode(), method="POST")
        r.add_header("X-Saturday-Token", TOKEN)
        r.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(r, timeout=120) as resp:
            resp.read()
        status, state = _req(srv.base, "/api/state")
        assert status == 200
        assert state["usage"]["turns"] >= 1
        assert any(m["model"].endswith(app.base_cfg.model or "?") for m in state["usage"]["models"])


def test_reusing_a_schedule_id_is_reported_as_a_replacement(tmp_path, monkeypatch, capsys):
    """Re-adding an id replaces the schedule; saying "added" hid that.

    A schedule is unattended automation, so a silently replaced one is a job
    that just stops running. Editing by re-adding stays supported - the exit
    code and the store behaviour are unchanged - but the output now names
    what it displaced."""
    from argparse import Namespace

    import saturday.config as cfgmod
    from saturday import cli

    monkeypatch.setattr(cfgmod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfgmod, "CONFIG_FILE", None)

    def add(expr, task):
        return cli.cmd_schedule(Namespace(
            schedule_cmd="add", id="job", expr=expr, task=task, model=None, provider=None))

    assert add("*/5 * * * *", "first") == 0
    out = capsys.readouterr().out
    assert "added schedule 'job'" in out and "replaced" not in out

    assert add("*/9 * * * *", "second") == 0          # still succeeds
    cap = capsys.readouterr()
    assert "replaced schedule 'job'" in cap.out
    assert "*/5 * * * *" in cap.err and "first" in cap.err   # names what it displaced

    rows = ScheduleStore(tmp_path / "schedules.json").list()
    assert [(r.id, r.expr, r.task) for r in rows] == [("job", "*/9 * * * *", "second")]


# --- routing: one ladder over agents and models -----------------------------

def _fake_providers(monkeypatch, tmp_path):
    """A two-provider world with a free and a metered tier, no network."""
    from saturday import catalog, routing

    monkeypatch.setattr(routing, "_db_path", lambda: tmp_path / "routing.db")
    monkeypatch.setattr(catalog, "providers", lambda timeout=8.0, only=None, refresh=False: [
        catalog.ProviderEntry(name="openrouter", configured=True, reachable=True, detail="ok",
                              models=["x/a:free", "x/b:free", "x/paid"], usable=True),
        catalog.ProviderEntry(name="deepseek", configured=True, reachable=True, detail="ok",
                              models=["ds-pro"], usable=True),
    ])
    return routing


def test_route_prefers_the_cheaper_tier(monkeypatch, tmp_path):
    routing = _fake_providers(monkeypatch, tmp_path)
    kind, target = routing.route()
    assert kind == "model"
    assert target.endswith(":free"), f"a free model should outrank a metered one, got {target}"


def test_a_payment_failure_parks_the_whole_tier_not_one_model(monkeypatch, tmp_path):
    """Parking a single model made auto walk every free model in turn.

    "Insufficient balance" is a fact about a provider's tier, so one failure
    has to remove its siblings too - otherwise the router re-learns the same
    thing once per model."""
    routing = _fake_providers(monkeypatch, tmp_path)
    assert routing.group_of("openrouter/x/a:free") == "openrouter:free"
    assert routing.group_of("openrouter/x/paid") == "openrouter:metered"
    assert routing.group_of("claude-code") == "claude-code", "agents stay individual"

    routing.mark_unusable("openrouter/x/a:free")
    left = {c.agent for c in routing.model_candidates()}
    assert "openrouter/x/a:free" not in left
    assert "openrouter/x/b:free" not in left, "the sibling free model must go too"
    assert "openrouter/x/paid" in left, "the metered tier is a different question"

    kind, target = routing.route()
    assert target != "openrouter/x/a:free"


def test_a_stale_backoff_never_disables_auto(monkeypatch, tmp_path):
    """With everything parked, routing must still choose something.

    Refusing would let a 15-minute-old failure switch the feature off, when
    the reason may well have been fixed since."""
    routing = _fake_providers(monkeypatch, tmp_path)
    for ident in ("openrouter/x/a:free", "openrouter/x/paid", "deepseek/ds-pro"):
        routing.mark_unusable(ident)
    assert routing.model_candidates() == [], "everything really is parked"
    choice = routing.route()
    assert choice is not None, "auto must still pick something rather than give up"
    assert choice[0] == "model"


def test_unusable_detection_reads_real_provider_errors():
    from saturday import routing

    assert routing.looks_unusable('{"error":{"message":"Insufficient Balance"}}')
    assert routing.looks_unusable("HTTP Error 402: Payment Required")
    assert routing.looks_unusable("401 Unauthorized")
    assert not routing.looks_unusable("connection reset by peer")


# --- routing: complexity-aware model choice ----------------------------------

def test_task_complexity_reads_the_task_not_a_hardcoded_list():
    from saturday.routing import COMPLEX, STANDARD, TRIVIAL, task_complexity

    trivial = ["open notion", "go to google.com and open the first result",
              "list the files in this directory", "check the weather",
              "take a screenshot"]
    complex_ = ["why is checkout conversion dropping, and what should we change?",
               "refactor the auth module for clarity and testability",
               "debug why the build fails intermittently on CI",
               "compare postgres and sqlite for this workload and recommend one"]
    standard = ["what is 2+2", "summarize this document", ""]
    for t in trivial:
        assert task_complexity(t) == TRIVIAL, t
    for t in complex_:
        assert task_complexity(t) == COMPLEX, t
    for t in standard:
        assert task_complexity(t) == STANDARD, t


def test_complex_task_reaches_past_the_cheapest_tier_for_a_priced_model(monkeypatch, tmp_path):
    """A real fetched price, not a guess: the more expensive model IS the
    provider's own flagship, and a task that reads as a real decision should
    reach it even though a cheaper tier was available."""
    from saturday import catalog, routing

    monkeypatch.setattr(routing, "_db_path", lambda: tmp_path / "routing.db")
    monkeypatch.setattr(catalog, "providers", lambda timeout=8.0, only=None, refresh=False: [
        catalog.ProviderEntry(name="prov", configured=True, reachable=True, detail="ok",
                              models=["cheap", "flagship"], usable=True),
    ])
    monkeypatch.setattr("saturday.usage.model_pricing",
                        lambda p, m: (10.0, 30.0) if m == "flagship" else (0.1, 0.2))

    trivial = routing.route(text="open the app")
    assert trivial == ("model", "prov/cheap"), "a routine task must not change"

    smart = routing.route(text="debug why the build fails and recommend a fix")
    assert smart == ("model", "prov/flagship"), "a real decision should reach the priced flagship"


def test_complex_task_falls_back_to_naming_hints_when_no_price_exists(monkeypatch, tmp_path):
    """DeepSeek's own /models has no pricing field at all - this is the shape
    that provider actually returns, so the naming convention is what has to
    carry a complex task there, not a fetched number."""
    from saturday import catalog, routing

    monkeypatch.setattr(routing, "_db_path", lambda: tmp_path / "routing.db")
    monkeypatch.setattr(catalog, "providers", lambda timeout=8.0, only=None, refresh=False: [
        catalog.ProviderEntry(name="deepseek", configured=True, reachable=True, detail="ok",
                              models=["deepseek-v4-flash", "deepseek-v4-pro"], usable=True),
    ])
    monkeypatch.setattr("saturday.usage.model_pricing", lambda p, m: None)

    assert routing.route(text="open a file") == ("model", "deepseek/deepseek-v4-flash")
    assert routing.route(text="debug why the build fails and recommend a fix") == (
        "model", "deepseek/deepseek-v4-pro"), "the -pro name should outrank -flash with no price data"


def test_complex_task_never_fabricates_a_ranking_with_no_signal_at_all(monkeypatch, tmp_path):
    """No price, no naming hint anywhere in the pool: falls through to the
    plain cheapest-first ladder rather than guess at an order."""
    from saturday import catalog, routing

    monkeypatch.setattr(routing, "_db_path", lambda: tmp_path / "routing.db")
    monkeypatch.setattr(catalog, "providers", lambda timeout=8.0, only=None, refresh=False: [
        catalog.ProviderEntry(name="prov", configured=True, reachable=True, detail="ok",
                              models=["x/a:free", "x/b"], usable=True),
    ])
    monkeypatch.setattr("saturday.usage.model_pricing", lambda p, m: None)

    assert routing.route(text="debug why the build fails") == ("model", "prov/x/a:free"), (
        "with no capability signal at all, the free tier must still win"
    )


def test_complexity_aware_false_forces_the_plain_ladder(monkeypatch, tmp_path):
    """The off switch: a caller (or a user's setting) that wants "auto" to
    never reach past the cheapest tier on its own judgment about the task."""
    from saturday import catalog, routing

    monkeypatch.setattr(routing, "_db_path", lambda: tmp_path / "routing.db")
    monkeypatch.setattr(catalog, "providers", lambda timeout=8.0, only=None, refresh=False: [
        catalog.ProviderEntry(name="prov", configured=True, reachable=True, detail="ok",
                              models=["cheap", "flagship"], usable=True),
    ])
    monkeypatch.setattr("saturday.usage.model_pricing",
                        lambda p, m: (10.0, 30.0) if m == "flagship" else (0.1, 0.2))

    picked = routing.route(text="debug why the build fails and recommend a fix",
                           complexity_aware=False)
    assert picked == ("model", "prov/cheap"), "complexity_aware=False must ignore the task text"


def test_naming_tier_hint_reads_vendor_conventions_not_model_names():
    from saturday.routing import _naming_tier_hint

    assert _naming_tier_hint("gpt-5-pro") > _naming_tier_hint("gpt-5-mini")
    assert _naming_tier_hint("claude-opus-5") > _naming_tier_hint("claude-haiku-4.5")
    assert _naming_tier_hint("deepseek-v4-pro") > _naming_tier_hint("deepseek-v4-flash")
    # a bare name with neither adjective carries no opinion either way
    assert _naming_tier_hint("gpt-5") == 0.0


def test_concurrent_schedule_writes_do_not_lose_entries(tmp_path):
    """S13: _load/_save is a read-modify-write and nothing guarded it, so the
    watcher's mark_fired and the UI's add landed on top of each other."""
    import threading

    from saturday.schedule import ScheduleStore

    path = tmp_path / "schedules.json"
    barrier = threading.Barrier(8)

    def add(n):
        barrier.wait()
        # a separate store per thread: the watcher and the web UI each build
        # their own over the same file, which is why the lock is per path
        ScheduleStore(path).add(f"s{n}", "* * * * *", f"task {n}")

    threads = [threading.Thread(target=add, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ids = {s.id for s in ScheduleStore(path).list()}
    assert ids == {f"s{i}" for i in range(8)}, f"lost writes: {sorted(ids)}"


def test_firing_a_removed_schedule_does_not_resurrect_it(tmp_path):
    """The watcher reads due() and then calls mark_fired, and the UI can
    remove the schedule in between.

    The `if sid in items` guard for this was already there and already
    correct; what was missing was the lock that makes the check meaningful,
    since without it mark_fired could load before the remove and save after
    it. This pins the sequential half of that contract - the concurrent half
    is what test_concurrent_schedule_writes_do_not_lose_entries covers."""
    from saturday.schedule import ScheduleStore

    path = tmp_path / "schedules.json"
    store = ScheduleStore(path)
    store.add("gone", "* * * * *", "do a thing")

    assert store.remove("gone") is True
    store.mark_fired("gone")

    assert [s.id for s in store.list()] == [], "the removed schedule came back"


def test_a_schedule_file_is_never_half_written(tmp_path, monkeypatch):
    """A truncated write does not lose one schedule, it loses all of them:
    _load returns {} on a JSON error."""
    import os
    from pathlib import Path

    from saturday.schedule import ScheduleStore

    path = tmp_path / "schedules.json"
    store = ScheduleStore(path)
    store.add("keep", "* * * * *", "important")

    seen = []
    real_replace = os.replace

    def watched(src, dst):
        # the destination still holds the previous good content right up to
        # the rename, which is the property write_text did not have
        seen.append(Path(dst).read_text(encoding="utf-8") if Path(dst).is_file() else "")
        return real_replace(src, dst)

    monkeypatch.setattr("saturday.schedule.os.replace", watched)
    store.add("second", "* * * * *", "another")

    assert seen and "important" in seen[0], "the file was overwritten in place"
    assert {s.id for s in store.list()} == {"keep", "second"}
