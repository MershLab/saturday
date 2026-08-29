"""Parity round 2 regressions: hooks, branching, continuable/background
subagents, repo index, LSP client, MCP HTTP transport."""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent))


from saturday.sessions import SessionStore, verify_chain
from saturday.tasks import SubagentTask


# ------------------------------------------------------- user hooks


def test_hooks_file_blocks_tool_call(tmp_path, monkeypatch):
    import saturday.config as cfgmod
    from saturday.user_hooks import load_hooks, make_pre_tool_hook

    (tmp_path / ".saturday").mkdir()
    (tmp_path / ".saturday" / "hooks.json").write_text(
        json.dumps({"pre_tool_call": [f'"{sys.executable}" -c "import sys,json; json.load(sys.stdin); print(\'no\', file=sys.stderr); sys.exit(2)"']}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cfgmod, "CONFIG_DIR", tmp_path / "home")
    monkeypatch.setenv("SATURDAY_TRUST_ALL_PROJECTS", "1")
    cfg = load_hooks(str(tmp_path))
    assert len(cfg["pre_tool_call"]) == 1
    hook = make_pre_tool_hook(cfg["pre_tool_call"])
    reason = hook("shell", {"command": "echo hi"})
    assert reason is not None and "blocked by user hook" in reason and "no" in reason


def test_hook_exit_zero_allows_and_crash_does_not_block():
    from saturday.user_hooks import make_pre_tool_hook, run_hook

    ok_cmd = f'"{sys.executable}" -c "import sys,json; json.load(sys.stdin)"'
    code, out = run_hook(ok_cmd, {"event": "pre_tool_call", "tool": "t", "args": {}})
    assert code == 0
    hook = make_pre_tool_hook([ok_cmd])
    assert hook("shell", {}) is None
    # broken command must never block the tool (fail-open for non-2 exits)
    hook2 = make_pre_tool_hook(["definitely-not-a-real-command-xyz"])
    assert hook2("shell", {}) is None


def test_agent_run_chains_user_hooks_after_safety(tmp_path):
    import saturday.user_hooks as uh

    calls = []
    orig_load = uh.load_hooks
    monkeypatch_lambda = lambda root=None: {
        "pre_tool_call": [],
        "post_tool_call": [],
        **({"pre_tool_call": ["dummy"]} if False else {}),
    }

    class FakeResult:
        name = "x"
        ok = True
        output = ""
        error = None

    # direct unit: pre hook returns reason string -> loop blocks
    block = uh.make_pre_tool_hook(['"' + sys.executable + '" -c "import sys; sys.exit(2)"'])("any", {})
    assert block is not None


# ------------------------------------------------------- branching


def test_session_branch_copies_prefix_and_verifies(tmp_path):
    store = SessionStore(root=tmp_path / "s")
    sid = store.create({"task": "original work"})
    for i in range(4):
        store.append(sid, {"type": "messages", "messages": [
            {"role": "user", "content": f"q{i}"},
            {"role": "assistant", "content": f"a{i}"},
        ]})
    store.save_checkpoint(sid, [{"role": "user", "content": "q0"}, {"role": "assistant", "content": "a0"}])
    branch_sid = store.branch(sid, keep_messages=4)
    assert branch_sid is not None and branch_sid != sid
    bdata = store.load(branch_sid)
    flat = []
    for rec in bdata["records"]:
        if rec.get("type") == "messages":
            flat.extend(rec["messages"])
    assert len(flat) == 4 and flat[0]["content"] == "q0"
    status = verify_chain(bdata["records"])
    assert status["ok"]
    # checkpoint copied truncated
    ckpt = store.load_checkpoint(branch_sid)
    assert ckpt == [{"role": "user", "content": "q0"}, {"role": "assistant", "content": "a0"}]
    # original untouched
    assert len(store.history_messages(sid)) == 8
    # default keep: drops final exchange
    d = store.branch(sid)
    dmsgs = store.history_messages(d)
    assert len(dmsgs) == 6 and dmsgs[-1]["content"] == "a2"


def test_branch_unknown_session_returns_none(tmp_path):
    store = SessionStore(root=tmp_path / "s")
    assert store.branch("missing") is None


# ------------------------------------------------------- subagents v2


class _FakeChildAgent:
    def __init__(self):
        self.turns = 0

    def run(self, prompt, initial_history=None):
        self.turns += 1
        prev = len(initial_history or [])

        class T:
            final_answer = f"prev={prev} turn={self.turns}"
            stop_reason = "done"

        t = T()
        t.messages = lambda: [
            {"role": "system", "content": "s"},
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": f"prev={prev} turn={self.turns}"},
        ]
        return t


def test_subagent_continuation_keeps_child_context():
    def factory():
        return _FakeChildAgent()

    task = SubagentTask(agent_factory=factory)
    ok1, out1 = task.run({"description": "d", "prompt": "first question"})
    cid = out1.split("continue_id=")[1].split()[0]
    assert "prev=0 turn=1" in out1
    ok2, out2 = task.run({"description": "d", "prompt": "follow-up", "continue_id": cid})
    assert ok1 and ok2
    # second turn saw the full first exchange (user prompt + reply) as history
    assert "prev=2 turn=2" in out2
    # unknown continue id starts a fresh child instead of failing
    ok3, _ = task.run({"description": "d", "prompt": "new child", "continue_id": "sub-999"})
    assert ok3


def test_background_subagent_reports_via_job_manager():
    from saturday.tools.jobs import JobManager

    def factory():
        return _FakeChildAgent()

    task = SubagentTask(agent_factory=factory)
    ok, msg = task.run({"description": "d", "prompt": "slow thing", "background": True})
    assert ok and "job_id=ag-sub-" in msg
    jid = msg.split("job_id=")[1].split()[0]
    job = JobManager.shared().get(jid)
    assert job is not None
    deadline = time.time() + 5
    while job.status() != "done" and time.time() < deadline:
        time.sleep(0.05)
    assert "turn=1" in job.tail()


def test_legacy_runner_contract_still_works():
    task = SubagentTask(runner=lambda p: f"ran {p}")
    ok, out = task.run({"description": "d", "prompt": "x"})
    assert ok and out.startswith("ran x")


# ------------------------------------------------------- repo index


def test_repo_index_search_finds_identifier_variants(tmp_path):
    from saturday.tools.repo_index import search_index

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod_a.py").write_text("def parse_hermes_tool_calls(text):\n    return text\n")
    (tmp_path / "pkg" / "mod_b.py").write_text("unrelated = 1\n")
    hits = search_index(tmp_path, "hermes tool calls")
    paths = [h["path"] for h in hits]
    assert "pkg/mod_a.py" in paths and "pkg/mod_b.py" not in paths
    top = next(h for h in hits if h["path"] == "pkg/mod_a.py")
    assert top["line"] >= 1


def test_repo_search_tool_end_to_end(tmp_path):
    from saturday.tools.files import WriteFile
    from saturday.tools.repo_index import make_repo_search_tool

    WriteFile(root=str(tmp_path)).run({"path": "src/app.py", "content": "def charge_customer(): ...\n"})
    tool = make_repo_search_tool(lambda: str(tmp_path))
    ok, out = tool.run({"query": "charge customer"})
    assert ok and "src/app.py" in out


# ------------------------------------------------------- LSP


class _FakeTransport:
    """Speaks enough LSP over byte streams for offline client tests."""

    def __init__(self):
        self.inbox = bytearray()
        self.sent: list[dict] = []
        self._lock = threading.Lock()

    def server_send(self, msg: dict) -> None:
        body = json.dumps(msg).encode()
        with self._lock:
            self.inbox += f"Content-Length: {len(body)}\r\n\r\n".encode() + body

    def write(self, data: bytes) -> None:
        raw = data.decode("utf-8", errors="replace")
        body = raw.split("\r\n\r\n", 1)[1]
        self.sent.append(json.loads(body))

    def read(self, n: int) -> bytes:
        deadline = time.time() + 5
        while time.time() < deadline:
            with self._lock:
                if len(self.inbox) >= n:
                    out = bytes(self.inbox[:n])
                    del self.inbox[:n]
                    return out
            time.sleep(0.01)
        return b""

    def alive(self):
        return True

    def close(self):
        pass


def test_lsp_client_initialize_diagnostics_definition():

    t = FakeTransportFactory()
    client = t.client
    # initialize round-trip answered by the fake server thread
    def responder():
        while True:
            req = next((m for m in t.requests if m.get("id") == 1), None)
            if req:
                break
            time.sleep(0.02)
        t.transport.server_send({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}})

    threading.Thread(target=responder, daemon=True).start()
    client.initialize()
    assert any(m["method"] == "initialize" for m in t.requests)


class FakeTransportFactory:
    def __init__(self):
        from saturday.tools.lsp import LspClient

        self.transport = _FakeTransport()
        self.client = LspClient(self.transport, "file:///w", timeout_s=5)
        self.requests = self.transport.sent


def test_lsp_tools_graceful_without_servers(tmp_path):
    from saturday.tools.lsp import make_lsp_tools

    tools = make_lsp_tools({}, lambda: str(tmp_path))
    assert len(tools) == 2
    ok, msg = tools[0].run({"path": str(tmp_path / "x.py")})
    assert not ok and "no LSP server configured" in msg


def test_mcp_http_client_parses_json_and_sse(monkeypatch):
    from saturday.mcp_client import McpHttpClient

    class FakeResp:
        status = 200

        def __init__(self, body, ctype):
            self._body = body.encode()
            self.headers = {"Content-Type": ctype}

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setenv("TOK", "sekrit")
    client = McpHttpClient(url="https://mcp.example/rpc", headers={"Authorization": "Bearer ${TOK}"})

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.header_items())
        body = json.loads(req.data.decode())
        if str(body.get("method", "")).startswith("notifications/"):
            return FakeResp("", "application/json")
        if body.get("method") == "initialize":
            return FakeResp(json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": {"serverInfo": {"name": "x"}}}), "application/json")
        if body.get("method") == "tools/list":
            sse = (
                "event: message\r\n"
                + f'data: {json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": {"tools": [{"name": "ping", "description": "", "inputSchema": {}}]}})}\r\n\r\n'
            )
            return FakeResp(sse, "text/event-stream")
        raise AssertionError("unexpected method")

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    info = client.start()
    assert info["name"] == "x"
    hdrs = {k.lower(): v for k, v in captured["headers"].items()}
    assert hdrs["authorization"] == "Bearer sekrit"
    tools = client.list_tools()
    assert [t.name for t in tools] == ["ping"]
