from __future__ import annotations

import argparse
import hmac
import json
import os
import subprocess
import sys
from pathlib import Path

from saturday.config import PROVIDERS, AgentConfig, save_config

MAX_BODY = 32 * 1024 * 1024  # parity with webui.MAX_BODY

BANNER = r"""
           _                 _
 ___  __ _| |_ _   _ _ __ __| | __ _ _   _
/ __|/ _` | __| | | | '__/ _` |/ _` | | | |
\__ \ (_| | |_| |_| | | | (_| | (_| | |_| |
|___/\__,_|\__|\__,_|_|  \__,_|\__,_|\__, |
                                     |___/
agentic harness :: deepseek x hermes lineage
"""


def _print(*args: str) -> None:
    print(*args, flush=True)


def _spawn_detached(args: argparse.Namespace) -> int:
    """Re-launch this exact run in a fully detached process; return immediately.

    Output goes to .saturday/bg/<id>.log; checkpointing under the same session id
    makes the run crash-safe and resumable via chat --resume."""
    import time as _t

    session_id = getattr(args, "session", None) or _t.strftime("bg-%Y%m%d-%H%M%S")
    log_dir = Path(".saturday") / "bg"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = (log_dir / f"{session_id}.log").resolve()

    argv = [sys.executable, "-m", "saturday"] + [
        a for a in sys.argv[1:] if a != "--detach"
    ] + ["--session", session_id]
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0x8) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
    with open(log_path, "ab") as log_fh:
        proc = subprocess.Popen(
            argv,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=True,
        )
    _print(f"[detached] pid={proc.pid} session={session_id}")
    _print(f"[log] {log_path}")
    _print(f"[follow] Get-Content -Wait {log_path}   |   resume later: saturday chat --resume {session_id}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from saturday.agent.core import Agent
    from saturday.ui import paint, Spinner
    from saturday.utils.env import load_env_file

    load_env_file(getattr(args, "env", None))

    if getattr(args, "background", False):
        os.environ["SATURDAY_BACKGROUND_ONLY"] = "true"
    ci = bool(getattr(args, "ci", False))
    if ci:
        # CI mode: non-interactive by construction - deny-mode approvals,
        # quiet output, structured result line, exit code reflects outcome.
        args.quiet = True
    if getattr(args, "detach", False):
        return _spawn_detached(args)

    def on_reasoning(delta: str) -> None:
        if not args.quiet:
            sys.stdout.write(paint(delta, "dim"))
            sys.stdout.flush()

    def on_text(delta: str) -> None:
        if not args.quiet:
            sys.stdout.write(delta)
            sys.stdout.flush()

    from saturday.sessions import RunState

    run_state: RunState | None = None

    def on_session_id(sid: str) -> None:
        nonlocal run_state
        from saturday.config import CONFIG_DIR

        run_state = RunState(CONFIG_DIR / "sessions", sid)
        run_state.start()

    def on_result(result) -> None:
        if run_state is not None:
            run_state.heartbeat()
        if not args.quiet:
            color = "green" if result.ok else "red"
            status = "ok" if result.ok else "ERROR"
            body = (result.output if result.ok else (result.error or ""))[:400]
            _print("\n" + paint(f"[tool:{result.name} {status}]", color) + " " + body + "\n")

    agent = Agent(cfg=AgentConfig.load(_overrides(args, ci=ci)))
    try:
        if args.quiet and sys.stdout.isatty():
            with Spinner("saturday working"):
                traj = agent.run(
                    args.task,
                    attachments=getattr(args, "images", None),
                    on_tool_result=on_result,
                    session_id=args.session,
                    on_session_id=on_session_id,
                )
        else:
            traj = agent.run(
                args.task,
                attachments=getattr(args, "images", None),
                on_text_delta=on_text,
                on_reasoning_delta=on_reasoning,
                on_tool_result=on_result,
                session_id=args.session,
                on_session_id=on_session_id,
            )
    except BaseException:
        if run_state is not None:
            run_state.mark_crashed()
        raise
    if run_state is not None:
        run_state.done()
    if not args.quiet:
        _print("\n" + "-" * 60)
    final = traj.final_answer or f"[no answer; stopped: {traj.stop_reason}]"
    from saturday.provenance import apply_visible_footer

    _print(apply_visible_footer(final, getattr(agent.cfg, "provenance_marking", "metadata")))
    try:
        from saturday.usage import record_usage

        record_usage(
            provider=agent.cfg.provider,
            model=agent.cfg.model or "?",
            session=args.session or "",
            steps=len(traj.steps),
            prompt_tokens=traj.usage.prompt_tokens,
            completion_tokens=traj.usage.completion_tokens,
            total_tokens=traj.usage.total_tokens,
            stop_reason=traj.stop_reason or "",
        )
    except Exception:
        pass
    if args.json_out:
        from saturday.provenance import stamp_record

        record = traj.to_jsonl_record()
        marking = getattr(agent.cfg, "provenance_marking", "metadata") or "metadata"
        if marking != "off":
            record = stamp_record(record, provider=agent.cfg.provider, model=agent.cfg.model or "", session_id=args.session or "")
        Path(args.json_out).write_text(json.dumps(record, indent=2), encoding="utf-8")
        _print(f"[trajectory saved -> {args.json_out}]")
    if ci:
        ok = traj.stop_reason == "done" and bool(traj.final_answer)
        _print(f"CI RESULT: {'PASS' if ok else 'FAIL'} stop_reason={traj.stop_reason} steps={len(traj.steps)} tokens={traj.usage.total_tokens}")
        return 0 if ok else 1
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    from saturday.agent.core import Agent
    from saturday.repl import Repl
    from saturday.utils.env import load_env_file

    load_env_file(getattr(args, "env", None))
    agent = Agent(cfg=AgentConfig.load(_overrides(args)))
    initial_history = None
    if getattr(args, "resume", None):
        from saturday.sessions import SessionStore

        store = SessionStore()
        initial_history = store.load_checkpoint(args.resume) or store.history_messages(args.resume)
        agent.restore_checkpoint_meta(store.load_checkpoint_meta(args.resume))
        if initial_history:
            _print(f"[resumed session {args.resume}: {len(initial_history)} messages]")
        else:
            _print(f"[no history found for {args.resume}; starting fresh]")
    return Repl(agent, initial_history=initial_history, resumed_id=getattr(args, "resume", None)).run()


def cmd_eval(args: argparse.Namespace) -> int:
    from saturday.agent.core import Agent
    from saturday.eval.builtin import builtin_suite
    from saturday.eval.runner import EvalRunner
    from saturday.utils.env import load_env_file

    load_env_file(getattr(args, "env", None))
    cfg = AgentConfig.load(_overrides(args))
    cases = builtin_suite(root=cfg.workspace_root)
    runner = EvalRunner(
        lambda: Agent(cfg=cfg),
        out_dir=args.out or "eval_runs",
        root=cfg.workspace_root,
    )
    results = runner.run(cases)
    summary = EvalRunner.summarize(results)
    _print(json.dumps(summary, indent=2))
    for r in results:
        mark = "PASS" if r.reward >= 0.999 else "FAIL"
        _print(f"{mark}  {r.case_id}  reward={r.reward:.2f} steps={r.steps} tokens={r.total_tokens}")
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    from saturday.tools import default_registry

    reg = default_registry(AgentConfig.load())
    for spec in reg.specs():
        _print(f"- {spec['name']}: {spec['description']}")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    """Interactive one-time setup: provider, API key, model — connection-tested."""
    import os as _os
    from getpass import getpass

    from saturday.config import PROVIDERS, get_config_dir, save_config
    from saturday.llm.probe import probe_connection
    from saturday.utils.env import load_env_file
    from saturday.webui_support import _env_upsert

    load_env_file(getattr(args, "env", None))

    def _ask(label: str, default: str = "") -> str:
        try:
            return input(label + (f"[Enter={default}] " if default else " ")).strip() or default
        except EOFError:
            raise SystemExit(1)

    cloud = sorted(n for n in PROVIDERS if n not in ("ollama", "vllm"))
    local = sorted(n for n in PROVIDERS if n in ("ollama", "vllm"))
    _print("Saturday setup — one minute, one time. We verify the connection before saving.\n")
    _print("cloud providers (API key):")
    for i, n in enumerate(cloud, 1):
        prof = PROVIDERS[n]
        tag = "  [key detected]" if prof.resolve_api_key() else ""
        _print(f"  {i:>2}. {n:<13} default: {prof.default_model}{tag}")
    _print("local providers (no key needed):")
    for n in local:
        _print(f"      {n:<13} default: {PROVIDERS[n].default_model}")
    try:
        choice = _ask("\nprovider (number or name)")
    except SystemExit:
        _print("\nsetup needs an interactive terminal — set the provider key in your")
        _print("environment instead (see `saturday doctor` for what it expects).")
        return 1

    prof = None
    if choice.isdigit() and 1 <= int(choice) <= len(cloud):
        prof = PROVIDERS[cloud[int(choice) - 1]]
    elif choice in PROVIDERS:
        prof = PROVIDERS[choice]
    if prof is None:
        _print(f"unknown provider '{choice}'")
        return 1

    needs_key = prof.name not in ("ollama", "vllm")
    key = prof.resolve_api_key() if needs_key else ""
    if needs_key and key:
        if _ask(f"using detected {prof.api_key_env} key", "y").lower() in ("y", "yes"):
            pass
        else:
            key = getpass(f"{prof.api_key_env} (paste, hidden): ")
    elif needs_key:
        key = getpass(f"{prof.api_key_env} (paste, hidden): ")
    if needs_key and not key:
        _print("no key supplied — nothing saved.")
        return 1

    ok, detail, models = False, "", []
    for attempt in range(2):
        ok, detail, models = probe_connection(prof, key)
        if ok:
            break
        _print(f"connection test failed: {detail}")
        if not needs_key:  # local server may just not be up yet
            _print("saving anyway — start your local server before chatting.")
            break
        if attempt == 0:
            key = getpass("paste the key again (hidden): ")
            if not key:
                return 1
    if needs_key and not ok and attempt and key:
        _print("saving anyway — run `saturday doctor` after fixing the key.")

    model = ""
    if models:
        shown = sorted(models)[:15]
        _print(f"\nprovider reports {len(models)} model(s):")
        for i, m in enumerate(shown, 1):
            _print(f"  {i:>2}. {m}")
        if len(models) > len(shown):
            _print(f"     ... {len(models) - len(shown)} more")
        pick = _ask("pick a model (number, or type an id, or Enter for provider default)")
        if pick.isdigit() and 1 <= int(pick) <= len(shown):
            model = shown[int(pick) - 1]
        elif pick:
            model = pick
    else:
        model = _ask("model (Enter for provider default)")

    if needs_key and key:
        _env_upsert(get_config_dir() / ".env", prof.api_key_env, key)
        _os.environ[prof.api_key_env] = key
    patch = {"provider": prof.name}
    if model:
        patch["model"] = model
    save_config(patch)

    _print(f"\nconfigured: {prof.name}" + (f" \u00b7 {model}" if model else " \u00b7 model: provider default"))
    model_line = model or prof.default_model
    if ok:
        _print(f"connection:   {detail}")
    else:
        _print("connection:   NOT VERIFIED — run `saturday doctor`")
    _print(f"key stored:   {get_config_dir() / '.env'}")
    _print("\nnext:  saturday run \"your task here\"   |   saturday chat   |   saturday app")
    _print(f"       (model: {model_line})")
    return 0


def _configured_or_hint(ns: argparse.Namespace) -> int | None:
    """Exit-code guard for commands that need a provider key that isn't set."""
    from saturday.config import AgentConfig
    from saturday.utils.env import load_env_file

    load_env_file(getattr(ns, "env", None))
    try:
        cfg = AgentConfig.load()
        prof = cfg.profile()
    except ValueError:
        return None  # invalid provider surfaces inside the command itself
    if prof.name in ("ollama", "vllm") or prof.resolve_api_key():
        return None
    _print(f"no API key configured for '{cfg.provider}' ({prof.api_key_env}).")
    _print("run `saturday setup` — pick a provider, paste the key, we verify the connection.")
    _print("or set it in your environment:  export {0}=sk-...".format(prof.api_key_env))
    return 1


def _print_first_run_nudge() -> None:
    from saturday.config import AgentConfig
    from saturday.utils.env import load_env_file

    load_env_file(None)
    try:
        cfg = AgentConfig.load()
        prof = cfg.profile()
    except ValueError:
        return
    if prof.name not in ("ollama", "vllm") and not prof.resolve_api_key():
        _print("\nfirst run? `saturday setup` — provider, key, model, connection-tested.")


def cmd_config(args: argparse.Namespace) -> int:
    if args.show:
        cfg = AgentConfig.load()
        profile = cfg.profile()
        _print(json.dumps({
            "provider": cfg.provider,
            "model": cfg.model,
            "base_url": profile.resolve_base_url(),
            "api_key_env": profile.api_key_env,
            "api_key_set": bool(profile.resolve_api_key()),
            "temperature": cfg.temperature,
            "max_steps": cfg.max_steps,
            "workspace_root": cfg.workspace_root,
        }, indent=2))
        return 0
    if args.set:
        partial = {}
        for pair in args.set:
            key, _, value = pair.partition("=")
            if key in ("temperature",):
                value = float(value)
            elif key in ("max_steps", "max_tokens"):
                value = int(value)
            partial[key] = value
        save_config(partial)
        _print(f"saved: {partial}")
        return 0
    _print("providers: " + ", ".join(PROVIDERS))
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from saturday.mcp_client import McpError, McpStdioClient
    from saturday.mcp_plugin import load_mcp_config

    problems: list[str] = []
    servers = load_mcp_config(getattr(args, "config", None), warnings=problems)
    for problem in problems:
        _print(f"warning: {problem}")
    servers.update(AgentConfig.load().mcp_servers or {})
    if args.server:
        servers = {k: v for k, v in servers.items() if k == args.server}
    if not servers:
        _print('no MCP servers configured; add .saturday/mcp.json: {"servers": {"alias": {"command": "...", "args": [...]}}}')
        return 0
    rc = 0
    for alias, spec in servers.items():
        command = [spec["command"]] + [str(a) for a in (spec.get("args") or [])]
        client = McpStdioClient(command=command, env=spec.get("env"))
        try:
            info = client.start()
            tools = client.list_tools()
            _print(f"{alias}: connected ({info.get('name', 'unknown')} {info.get('version', '')}".rstrip() + ")")
            for t in tools:
                _print(f"  - {t.name}: {(t.description or '')[:90]}")
        except McpError as exc:
            _print(f"{alias}: FAILED - {exc}")
            rc = 1
        finally:
            client.close()
    return rc


def cmd_doctor(args: argparse.Namespace) -> int:
    import sys as _sys

    from saturday.llm.probe import probe_connection
    from saturday.utils.env import load_env_file

    load_env_file(getattr(args, "env", None))
    failures = 0
    cfg = AgentConfig.load(_overrides(args))

    if getattr(args, "privacy", False):
        from saturday.config import CONFIG_DIR

        _print("Saturday data-flow report (--privacy)")
        _print("-" * 46)
        _print("leaves this machine : chat text + tool results you send, sent to the")
        _print("                      configured LLM provider endpoint over HTTPS.")
        try:
            prof = cfg.profile()
            _print(f"endpoint            : {prof.resolve_base_url()} ({cfg.provider})")
        except ValueError:
            _print("endpoint            : (invalid provider configured)")
        _print("stored locally      :")
        _print(f"  config            : {CONFIG_DIR / 'config.json'}")
        _print(f"  api keys          : {CONFIG_DIR / '.env'} (+ process env; never logged)")
        _print(f"  sessions          : {CONFIG_DIR / 'sessions'} (JSONL, tamper-evident chain)")
        _print(f"  memory            : {CONFIG_DIR / 'MEMORY.md'} + per-project .saturday/MEMORY.md")
        _print("  db backups        : <workspace>/.saturday/backup (guardrail snapshots)")
        _print("telemetry           : none - no analytics, no crash reporting, no phone-home")
        _print("web tools           : web_search/web_fetch contact public internet directly;")
        _print("                      queries are not proxied through any Saturday service")
        return 0

    _print(f"python        : {_sys.version.split()[0]} " + ("ok" if _sys.version_info >= (3, 10) else "TOO OLD"))
    if _sys.version_info < (3, 10):
        failures += 1

    try:
        profile = cfg.profile()
        _print(f"provider      : {cfg.provider} ({profile.resolve_base_url()})")
        _print(f"model         : {cfg.model}")
    except ValueError as exc:
        _print(f"provider      : FAIL - {exc}")
        return 1

    key = profile.resolve_api_key()
    needs_key = profile.name not in ("ollama", "vllm")
    if needs_key and not key:
        _print(f"api key       : MISSING ({profile.api_key_env})")
        failures += 1
    else:
        _print("api key       : present" if key or not needs_key else "api key       : n/a (local provider)")

    # --offline skips the probe entirely (CI/smoke: a provider that isn't
    # running must not fail the harness check)
    if getattr(args, "offline", False):
        ok, detail = True, "skipped (--offline)"
    else:
        ok, detail, _models = probe_connection(profile, key, timeout=8)
    if ok:
        _print(f"endpoint      : {detail}")
    elif "auth rejected" in detail:
        _print("endpoint      : reachable (auth rejected -> check key)")
        failures += 1
    elif needs_key and not key:
        _print("endpoint      : unverified (no key; expected for cloud providers)")
    elif detail.startswith("endpoint answered with HTTP "):
        _print(f"endpoint      : reachable ({detail.removeprefix('endpoint answered with ')})")
    else:
        _print(f"endpoint      : UNREACHABLE - {detail}")
        failures += 1

    ws = Path(cfg.workspace_root)
    try:
        ws.mkdir(parents=True, exist_ok=True)
        probe_file = ws / ".saturday-write-test"
        probe_file.write_text("ok", encoding="utf-8")
        probe_file.unlink()
        _print(f"workspace     : writable ({ws})")
    except OSError as exc:
        _print(f"workspace     : NOT WRITABLE - {exc}")
        failures += 1

    try:
        from saturday.tools import default_registry

        n = len(default_registry(cfg).names())
        _print(f"tools         : {n} registered")
    except Exception as exc:
        _print(f"tools         : FAILED to build registry - {exc}")
        failures += 1

    guardrails = bool(getattr(cfg, "destructive_guardrails", True))
    _print(f"guardrails    : {'on - irreversible data ops ask + db files auto-backed-up' if guardrails else 'OFF (destructive_guardrails=false)'}")
    mode = getattr(cfg, "persona_mode", "agent") or "agent"
    if mode == "assistant":
        _print("mode          : personal assistant (curated toolset)")

    # local config files must parse or every surface silently falls back to
    # defaults — surface that here instead of letting users discover it late
    from saturday.config import get_config_dir

    home = get_config_dir()
    for name in ("hooks.json", "approvals.json", "config.json"):
        p = home / name
        if p.is_file():
            try:
                json.loads(p.read_text(encoding="utf-8-sig"))
                _print(f"{name:<13} : ok")
            except (json.JSONDecodeError, OSError) as exc:
                _print(f"{name:<13} : INVALID JSON - {exc}")
                failures += 1

    if failures:
        _print(f"\n{failures} problem(s) found.")
        return 1
    _print("\nall checks passed; ready to run: saturday run \"your task\"")
    return 0


def cmd_tui(args: argparse.Namespace) -> int:
    from saturday.agent.core import Agent
    from saturday.repl import Repl
    from saturday.utils.env import load_env_file

    load_env_file(getattr(args, "env", None))
    agent = Agent(cfg=AgentConfig.load(_overrides(args)))
    return Repl(agent, tui=True).run()


def cmd_gateway(args: argparse.Namespace) -> int:
    from saturday.gateway import TelegramGateway, build_gateway_agent
    from saturday.utils.env import load_env_file

    load_env_file(getattr(args, "env", None))
    token = args.token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        _print("error: --token or TELEGRAM_BOT_TOKEN required")
        return 1
    # A gateway is remote control of a full-capability agent on this machine;
    # it must never accept arbitrary senders by default.
    allowed: set[int] | None
    if getattr(args, "allow_all", False):
        allowed = None
        _print("WARNING: --allow-all set - ANY Telegram user will be able to run commands on this machine.")
    elif args.allow:
        allowed = {int(c) for c in args.allow.split(",") if c.strip().lstrip("-").isdigit()}
        if not allowed:
            _print("error: --allow produced no valid chat ids")
            return 1
    else:
        _print("error: refusing to start an unauthenticated gateway.")
        _print("pass --allow <chat_id[,chat_id...]> to restrict who can talk to the agent,")
        _print("or --allow-all to accept ANY Telegram user (strongly discouraged).")
        return 1
    gw = TelegramGateway(token, build_gateway_agent(_overrides(args)), allowed_chat_ids=allowed)
    _print(f"telegram gateway polling (allowed chats: {sorted(allowed) if allowed else 'ALL'}); Ctrl-C to stop")
    try:
        gw.run_forever()
    except KeyboardInterrupt:
        pass
    return 0


def handle_message_payload(payload: dict, run_fn) -> dict:
    """Pure handler for POST /message bodies so routing logic is unit-testable.

    run_fn(text, initial_history, session_id) -> Trajectory-like object.
    """
    text = str(payload.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "missing 'text'"}
    session_id = str(payload.get("session_id") or "").strip()
    initial_history = None
    store = None
    if session_id:
        from saturday.sessions import SessionStore

        store = SessionStore()
        try:
            initial_history = store.load_checkpoint(session_id)
        except OSError:
            initial_history = None
    try:
        traj = run_fn(text, initial_history, session_id or None)
        out = {"ok": True, "answer": traj.final_answer, "stop_reason": traj.stop_reason}
        if session_id:
            out["session_id"] = session_id
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return out


def make_serve_handler(*, token: str, hosts: set[str] | None = None, origins: set[str] | None = None, agent_factory):
    """Build the POST /message handler with auth + Host/Origin pinning.

    The endpoint drives a full-capability agent, so it is never implicitly
    trustable: requests must carry the access token, present an allowed Host
    header (DNS-rebinding defense), and - when Origin is set at all - come
    from an allowed origin (drive-by CSRF defense). All three are exposed as
    class attributes so callers can finalize them once the bind port is known."""
    from http.server import BaseHTTPRequestHandler

    from saturday.utils.httpd import authority_allowed

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        access_token: str = ""
        allowed_hosts: set[str] = set()
        allowed_origins: set[str] = set()

        def log_message(self, fmt, *a):
            pass

        def _reply(self, status: int, obj: dict) -> None:
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            if not self.access_token:
                return True
            if f"k={self.access_token}" in (self.path or ""):
                return True
            # constant-time compares: header bytes are attacker-controlled, a
            # plain == leaks token prefixes via timing; encode() sidesteps
            # compare_digest's ASCII-only str restriction on odd headers
            supplied = (self.headers.get("X-Saturday-Token") or "").encode("utf-8")
            if hmac.compare_digest(supplied, self.access_token.encode("utf-8")):
                return True
            auth = (self.headers.get("Authorization") or "").encode("utf-8")
            return hmac.compare_digest(auth, f"Bearer {self.access_token}".encode("utf-8"))

        def do_POST(self):
            if not self._authorized():
                self._reply(401, {"error": "unauthorized: missing or invalid access token"})
                return
            if self.allowed_hosts and not authority_allowed(self.headers.get("Host") or "", self.allowed_hosts):
                self._reply(403, {"error": "rejected: Host header not allowed"})
                return
            origin = self.headers.get("Origin")
            if origin and self.allowed_origins and not authority_allowed(origin, self.allowed_origins):
                self._reply(403, {"error": "rejected: cross-origin requests are not allowed"})
                return
            # parity with webui._read_json: cap body size (413) and guard the
            # header parse (400) so a hostile Content-Length can't OOM or 500
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._reply(400, {"error": "invalid Content-Length"})
                return
            if n > MAX_BODY:
                self._reply(413, {"error": "request body too large"})
                return
            try:
                payload = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                self._reply(400, {"error": "invalid JSON body"})
                return
            if self.path.split("?", 1)[0] != "/message" or not isinstance(payload, dict) or not str(payload.get("text") or "").strip():
                self._reply(404, {"error": "not found"})
                return

            def run_fn(text, initial_history, session_id):
                agent = agent_factory()
                return agent.run(text, initial_history=initial_history, session_id=session_id)

            out = handle_message_payload(payload, run_fn)
            self._reply(200, out)

    Handler.access_token = token
    if hosts:
        Handler.allowed_hosts = set(hosts)
    if origins:
        Handler.allowed_origins = set(origins)
    return Handler


def cmd_serve(args: argparse.Namespace) -> int:
    import secrets

    from saturday.agent.core import Agent
    from saturday.config import AgentConfig
    from saturday.utils.env import load_env_file
    from saturday.utils.httpd import allowed_hosts, allowed_origins
    from http.server import ThreadingHTTPServer

    load_env_file(getattr(args, "env", None))

    token = ""
    if not getattr(args, "no_token", False):
        token = getattr(args, "token", None) or secrets.token_hex(16)
    handler = make_serve_handler(token=token, agent_factory=lambda: Agent(cfg=AgentConfig.load(_overrides(args))))
    srv = ThreadingHTTPServer((args.host, args.port), handler)
    srv.daemon_threads = True
    bound_host, bound_port = srv.server_address[:2]
    handler.allowed_hosts = allowed_hosts(bound_host, bound_port)
    handler.allowed_origins = allowed_origins(handler.allowed_hosts)
    display_host = "127.0.0.1" if args.host in ("0.0.0.0", "") else args.host
    _print(f"serving /message on http://{display_host}:{bound_port} (Ctrl-C to stop)")
    if token:
        _print("auth           : send 'Authorization: Bearer <token>' (or X-Saturday-Token / ?k=)")
        _print(f"token          : {token}" if getattr(args, "token", None) else "token          : (random per launch)")
    else:
        _print("WARNING        : --no-token set; anyone able to reach this port can run commands on this machine.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


def cmd_app(args: argparse.Namespace) -> int:
    from saturday.webui import serve as webui_serve

    return webui_serve(
        host=args.host,
        port=args.port,
        open_window=not args.no_window,
        width=args.width,
        height=args.height,
        # empty string = auth disabled (AppServer contract); None = generate.
        # Mapping --no-token to None made serve() mint a token anyway, so the
        # flag silently did nothing.
        token="" if args.no_token else (args.token or None),
        cfg_overrides=_overrides(args),
        env_path=getattr(args, "env", None),
    )


def cmd_sessions(args: argparse.Namespace) -> int:
    from saturday.sessions import SessionStore

    rows = SessionStore().list_sessions()
    if not rows:
        _print("no sessions yet")
        return 0
    for r in rows:
        _print(f"{r['id']}  {r['task']}")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    """Tamper-evidence for session history: verify hash chains, export bundles."""
    from saturday.sessions import SessionStore

    store = SessionStore(args.root) if getattr(args, "root", None) else SessionStore()
    if args.session_id:
        status = store.audit_verify(args.session_id)
        if status is None:
            _print(f"unknown session: {args.session_id}")
            return 1
        if args.export:
            from saturday.provenance import stamp_record

            bundle = store.audit_export(args.session_id)
            marking = getattr(AgentConfig.load(), "provenance_marking", "metadata") or "metadata"
            if bundle is not None and marking != "off":
                cfg = AgentConfig.load()
                bundle = stamp_record(bundle, provider=cfg.provider, model=cfg.model or "", session_id=args.session_id)
            out = Path(args.export)
            out.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
            _print(f"exported audit bundle -> {out}")
        mark = "OK" if status["ok"] else f"TAMPERED at record {status['broken_at']}"
        _print(
            f"{args.session_id}: chain {mark}  "
            f"({status['records']} records, {status['hashed']} hashed"
            + (f", {status['legacy']} legacy" if status.get("legacy") else "")
            + ")"
        )
        return 0 if status["ok"] else 1
    rows = store.list_sessions()
    if not rows:
        _print("no sessions yet")
        return 0
    bad = 0
    for r in rows:
        status = store.audit_verify(r["id"]) or {}
        ok = status.get("ok")
        if ok is False:
            bad += 1
        mark = {True: "ok", False: "TAMPERED", None: "?"}[ok]
        legacy = f", {status['legacy']} legacy" if status.get("legacy") else ""
        _print(f"{r['id']}  chain={mark}  {status.get('records', 0)} records{legacy}")
    return 1 if bad else 0


def cmd_export(args: argparse.Namespace) -> int:
    from saturday.plugins import core_plugin, install_plugins, learning_plugin, workflow_plugin
    from saturday.tools.base import ToolRegistry

    src = Path(args.dir or "eval_runs")
    reg = ToolRegistry()
    persona: list[str] = []
    install_plugins(reg, [core_plugin(None), workflow_plugin(), learning_plugin()], persona)
    known = set(reg.names()) | {"task", "finish"}
    records = []
    dropped = 0
    for p in sorted(src.glob("*.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        used_tools = {
            tc.get("function", {}).get("name")
            for m in rec.get("messages", [])
            for tc in (m.get("tool_calls") or [])
        }
        if not args.keep_unknown and used_tools - known:
            dropped += 1
            continue
        records.append(rec)
    marking = getattr(AgentConfig.load(), "provenance_marking", "metadata") or "metadata"
    if getattr(args, "compress", None):
        from saturday.eval.compress import compress_record

        records = [compress_record(r, int(args.compress)) for r in records]
        # compression REWRITES messages: it must run BEFORE stamping or the
        # shipped record's content hash would never match its own payload
    if marking != "off":
        from saturday.provenance import stamp_record

        cfg = AgentConfig.load()
        records = [
            stamp_record(r, provider=cfg.provider, model=cfg.model or "", session_id="")
            if "provenance" not in r
            else r
            for r in records
        ]
    out = Path(args.out)
    if getattr(args, "images", False):
        from saturday.exporter import embed_assets

        assets_dir = out.parent / (out.name + ".assets")
        copied = embed_assets(records, assets_dir)
        if copied:
            _print(f"embedded {copied} screenshot(s) -> {assets_dir}")
    with out.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    _print(f"exported {len(records)} trajectories -> {out}" + (f" ({dropped} dropped: unknown tools)" if dropped else ""))
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    """Scheduled automations: add/list/remove/watch (Hermes cron parity)."""
    from saturday.schedule import ScheduleStore, watch

    store = ScheduleStore()
    cmd = getattr(args, "schedule_cmd", "list")
    if cmd == "add":
        try:
            s = store.add(args.id, args.expr, args.task, model=args.model or "", provider=args.provider or "")
        except ValueError as exc:
            _print(f"error: {exc}")
            return 1
        _print(f"added schedule '{s.id}': {s.expr} -> {s.task[:80]}")
        return 0
    if cmd == "remove" or cmd == "rm":
        if not store.remove(args.id):
            _print(f"error: no schedule named '{args.id}'")
            return 1
        _print(f"removed schedule '{args.id}'")
        return 0
    if cmd == "watch":
        watch()
        return 0
    rows = store.list()
    if not rows:
        _print("no schedules. add one:  saturday schedule add '0 9 * * 1-5' 'standup notes'")
        return 0
    for s in rows:
        flags = []
        if s.model:
            flags.append(s.model)
        if s.provider:
            flags.append(s.provider)
        tail = " | " + ",".join(flags) if flags else ""
        _print(f"{s.id}  {s.expr}  {s.task[:100]}{tail}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Run the project's test suite(s) like `hermes verify` (recipe detection)."""
    from saturday.verify import detect_project, run_verification

    root = Path(getattr(args, "path", None) or ".")
    detections = detect_project(root)
    if not detections:
        _print(f"no project recipes detected in {root.resolve()} (pytest / npm / cargo / go / make)")
        return 0
    if getattr(args, "list_only", False):
        for label, _ in detections:
            _print(label)
        return 0
    results = run_verification(root, detections, timeout=float(getattr(args, "timeout", 600)))
    failed = 0
    for label, ok, tail in results:
        status = "OK  " if ok else "FAIL"
        _print(f"[{status}] {label}")
        if not ok and tail:
            _print("    " + tail.replace("\n", "\n    ")[-1200:])
        if not ok:
            failed += 1
    _print(f"{len(results) - failed}/{len(results)} suites passed")
    return 0 if failed == 0 else 1


def cmd_init(args: argparse.Namespace) -> int:
    """Scaffold Saturday project files in the current directory (idempotent)."""
    root = Path.cwd()
    force = bool(getattr(args, "force", False))
    created: list[str] = []
    skipped: list[str] = []

    def _write(rel: str, content: str) -> None:
        p = root / rel
        if p.exists() and not force:
            skipped.append(rel)
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        created.append(rel)

    _write(
        ".saturday/mcp.json.example",
        json.dumps(
            {
                "servers": {
                    "filesystem": {
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
                    }
                }
            },
            indent=2,
        )
        + "\n",
    )
    _write(
        ".saturday/hooks.json.example",
        json.dumps(
            {
                "pre_tool_call": [],
                "post_tool_call": ["echo done"],
            },
            indent=2,
        )
        + "\n",
    )
    agents = root / "AGENTS.md"
    if agents.exists() and not force:
        skipped.append("AGENTS.md")
    else:
        agents.write_text(
            "# Project instructions for Saturday\n\n"
            "<!-- This file is autoloaded into every Saturday session in this\n"
            "     workspace (CLAUDE.md is also honored; project file wins).\n"
            "     Keep it short and factual - it counts against context. -->\n\n"
            "## House rules\n"
            "- <e.g. Always run `pytest -q` after touching src/>.>\n"
            "- <e.g. Python 3.10+ only, stdlib in src/saturday.>\n\n"
            "## Notes\n"
            "- Copy .saturday/mcp.json.example to mcp.json to enable MCP servers.\n"
            "- Copy .saturday/hooks.json.example to hooks.json for lifecycle hooks.\n",
            encoding="utf-8",
        )
        created.append("AGENTS.md")

    if created:
        _print("created: " + ", ".join(created))
    if skipped:
        _print("kept existing (--force overwrites): " + ", ".join(skipped))
    _print("")
    _print("next steps:")
    _print('  saturday doctor                 # verify key/provider/workspace')
    _print('  saturday run "your task"        # one-shot task')
    _print("  saturday app                    # desktop UI")
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    from saturday import __version__

    _print(__version__)
    return 0


def _overrides(args: argparse.Namespace, ci: bool = False) -> dict:
    # contract: absent flags stay present as None (pinned by tests);
    # AgentConfig.load ignores None values itself
    out = {
        "provider": getattr(args, "provider", None),
        "model": getattr(args, "model", None),
        "temperature": getattr(args, "temperature", None),
        "max_steps": getattr(args, "max_steps", None),
        "persona_mode": "assistant" if getattr(args, "assistant", False) else None,
        "plan_mode": True if getattr(args, "plan", False) else None,
        "max_run_tokens": getattr(args, "max_run_tokens", None),
        "disabled_tools": getattr(args, "disabled_tools", None),
        "safety_mode": "autonomous" if getattr(args, "yolo", False) else None,
    }
    if ci:
        out["safety_mode"] = "deny"
        out["max_steps"] = min(out["max_steps"] or 25, 25)
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="saturday", description="Saturday agentic harness")
    parser.add_argument("--version", action="store_true")
    sub = parser.add_subparsers(dest="command")

    def common(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--provider", choices=sorted(PROVIDERS), help="LLM provider")
        p.add_argument("--model", help="model name override")
        p.add_argument("--temperature", type=float)
        p.add_argument("--max-steps", type=int, dest="max_steps")
        p.add_argument("--assistant", action="store_true", help="personal assistant mode: curated everyday toolset + assistant persona")
        p.add_argument("--env", help="path to .env file")
        return p

    p_run = sub.add_parser("run", help="run a one-shot task")
    common(p_run)
    p_run.add_argument("--session", metavar="SESSION_ID", help="persist checkpoints under this session id")
    p_run.add_argument("--ci", action="store_true", help="CI mode: non-interactive (deny approvals), quiet, prints 'CI RESULT: PASS|FAIL', exit 1 on failure")
    p_run.add_argument("--plan", action="store_true", help="plan mode: read-only tools only; agent outputs an implementation plan instead of executing")
    p_run.add_argument("--disable", dest="disabled_tools", help="comma-separated tools/families to turn off (e.g. web,computer_use; families: web, browser, computer_use, shell, python, file_writes, subagents, memory)")
    p_run.add_argument("--max-run-tokens", type=int, dest="max_run_tokens", help="abort the run once cumulative tokens exceed this (hard spend policy)")
    p_run.add_argument("--detach", action="store_true", help="run in a detached background process; returns immediately (log under .saturday/bg/)")
    p_run.add_argument("--background", action="store_true", help="background-only desktop mode: blocks pointer/keyboard/focus, forces non-intrusive UI Automation (pairs well with --detach)")
    p_run.add_argument("--image", action="append", default=None, dest="images", metavar="PATH", help="attach image (repeatable; vision models)")
    p_run.add_argument("--yolo", action="store_true", help="fully autonomous: NO approval prompts (dangerous patterns, guardrails and file-edit gates all auto-approved; hardline + deny rules still block)")
    p_run.add_argument("--json-out", dest="json_out", help="save trajectory JSON")
    p_run.add_argument("-q", "--quiet", action="store_true", help="only print final answer")
    p_run.add_argument("task", help="task description")
    p_run.set_defaults(fn=cmd_run)

    p_mcp = sub.add_parser("mcp", help="inspect configured MCP servers")
    p_mcp.add_argument("--config", help="path to mcp.json")
    p_mcp.add_argument("--server", help="only this alias")
    p_mcp.set_defaults(fn=cmd_mcp)

    p_chat = sub.add_parser("chat", help="interactive REPL session")
    common(p_chat)
    p_chat.add_argument("--disable", dest="disabled_tools", help="comma-separated tools/families to turn off for this session")
    p_chat.add_argument("--resume", metavar="SESSION_ID", help="continue a saved session")
    p_chat.add_argument("--yolo", action="store_true", help="fully autonomous: no approval prompts this session (/yolo toggles)")
    p_chat.set_defaults(fn=cmd_chat)

    p_sessions = sub.add_parser("sessions", help="list saved sessions")
    p_sessions.set_defaults(fn=cmd_sessions)

    p_tui = sub.add_parser("tui", help="full-screen console (alt-screen UI)")
    common(p_tui)
    p_tui.set_defaults(fn=cmd_tui)

    p_gw = sub.add_parser("gateway", help="run the Telegram gateway (long polling)")
    common(p_gw)
    p_gw.add_argument("--token", help="bot token (or TELEGRAM_BOT_TOKEN)")
    p_gw.add_argument("--allow", help="comma-separated chat ids allowed to use the agent (required unless --allow-all)")
    p_gw.add_argument("--allow-all", action="store_true", help="accept ANY Telegram user (dangerous; overrides --allow)")
    p_gw.set_defaults(fn=cmd_gateway)

    p_serve = sub.add_parser("serve", help="HTTP server exposing POST /message {text}")
    common(p_serve)
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8787)
    p_serve.add_argument("--token", help="require this access token (default: random per launch)")
    p_serve.add_argument("--no-token", action="store_true", help="disable the access token (local bind only; dangerous)")
    p_serve.set_defaults(fn=cmd_serve)

    p_app = sub.add_parser("app", help="desktop app UI (native window over the local agent)")
    common(p_app)
    p_app.add_argument("--host", default="127.0.0.1")
    p_app.add_argument("--port", type=int, default=8679)
    p_app.add_argument("--no-window", action="store_true", help="do not launch an app window; just print the URL")
    p_app.add_argument("--width", type=int, default=1220, help="app window width")
    p_app.add_argument("--height", type=int, default=840, help="app window height")
    p_app.add_argument("--token", help="fixed access token (default: random per launch)")
    p_app.add_argument("--no-token", action="store_true", help="disable local access token")
    p_app.add_argument("--yolo", action="store_true", help="start in fully-autonomous mode (no approval prompts; toggleable via the safety badge)")
    p_app.set_defaults(fn=cmd_app)

    p_doc = sub.add_parser("doctor", help="preflight checks: config, keys, endpoint, workspace")
    common(p_doc)
    p_doc.add_argument("--privacy", action="store_true", help="print a data-flow report: what leaves this machine and what stays local")
    p_doc.add_argument("--offline", action="store_true", help="skip the endpoint probe (CI/smoke: verify the harness without a running provider)")
    p_doc.set_defaults(fn=cmd_doctor)

    p_eval = sub.add_parser("eval", help="run verifiable eval suite")
    common(p_eval)
    p_eval.add_argument("--out", default="eval_runs", help="directory for trajectories")
    p_eval.set_defaults(fn=cmd_eval)

    p_tools = sub.add_parser("tools", help="list built-in tools")
    p_tools.set_defaults(fn=cmd_tools)

    p_init = sub.add_parser("init", help="scaffold AGENTS.md + .saturday config examples in this directory")
    p_init.add_argument("--force", action="store_true", help="overwrite existing scaffolded files")
    p_init.set_defaults(fn=cmd_init)

    p_audit = sub.add_parser("audit", help="verify tamper-evident session chains; export audit bundles")
    p_audit.add_argument("session_id", nargs="?", help="verify a specific session (default: list all)")
    p_audit.add_argument("--export", metavar="PATH", help="export a signed audit bundle as JSON")
    p_audit.add_argument("--root", metavar="DIR", help="session store root (default: ~/.saturday/sessions)")
    p_audit.set_defaults(fn=cmd_audit)

    p_cfg = sub.add_parser("config", help="show/save configuration")
    p_cfg.add_argument("--show", action="store_true")
    p_cfg.add_argument("--set", nargs="+", metavar="KEY=VALUE")
    p_cfg.set_defaults(fn=cmd_config)

    p_setup = sub.add_parser("setup", help="interactive first-run setup: provider + API key + model, connection-tested")
    p_setup.add_argument("--env", help="path to .env file")
    p_setup.set_defaults(fn=cmd_setup)

    p_export = sub.add_parser("export", help="merge trajectory JSONs into JSONL dataset")
    p_export.add_argument("--dir", default="eval_runs")
    p_export.add_argument("--out", default="trajectories.jsonl")
    p_export.add_argument("--keep-unknown", action="store_true", help="keep trajectories using unregistered tools")
    p_export.add_argument(
        "--compress",
        type=int,
        metavar="TOKENS",
        default=None,
        help="token-targeted compression: older tool results become short omission markers",
    )
    p_export.add_argument(
        "--images",
        action="store_true",
        help="embed every captured screenshot into <out>.assets/ and rewrite references (dataset-ready image+action pairs)",
    )
    p_export.set_defaults(fn=cmd_export)

    p_sched = sub.add_parser("schedule", help="cron-scheduled automations: add/list/remove/watch")
    sched_sub = p_sched.add_subparsers(dest="schedule_cmd", metavar="{add,list,remove,watch}")
    p_add = sched_sub.add_parser("add", help="add a schedule: '<min hour dom month dow>' '<task>'")
    p_add.add_argument("expr", help="5-field cron, e.g. '0 9 * * 1-5'")
    p_add.add_argument("task", help="task text for the agent to run")
    p_add.add_argument("--id", help="schedule id (default: sched-<timestamp>)")
    p_add.add_argument("--model", help="model override for this schedule")
    p_add.add_argument("--provider", help="provider override for this schedule")
    p_add.set_defaults(fn=cmd_schedule)
    p_list = sched_sub.add_parser("list", help="list schedules")
    p_list.set_defaults(fn=cmd_schedule)
    p_rm = sched_sub.add_parser("remove", help="remove a schedule")
    p_rm.add_argument("id")
    p_rm.set_defaults(fn=cmd_schedule)
    p_watch = sched_sub.add_parser("watch", help="run due schedules until stopped (Ctrl-C)")
    p_watch.set_defaults(fn=cmd_schedule)

    p_verify = sub.add_parser("verify", help="run the project's test suites (pytest/npm/cargo/go/make)")
    p_verify.add_argument("path", nargs="?", default=".", help="project directory (default: current)")
    p_verify.add_argument("--list", dest="list_only", action="store_true", help="detect and list recipes only")
    p_verify.add_argument("--timeout", type=float, default=600.0, help="per-suite timeout seconds")
    p_verify.set_defaults(fn=cmd_verify)

    return parser


def main(argv: list[str] | None = None) -> int:
    from saturday.ui import enable_ansi

    enable_ansi()
    parser = build_parser()
    ns = parser.parse_args(argv)
    if ns.version:
        from saturday import __version__

        _print(__version__)
        return 0
    if not getattr(ns, "command", None):
        _print(BANNER)
        parser.print_help()
        _print_first_run_nudge()
        return 0
    if getattr(ns, "command", None) in ("run", "chat", "tui"):
        code = _configured_or_hint(ns)
        if code is not None:
            return code
    try:
        return ns.fn(ns)
    except KeyboardInterrupt:
        _print("\n[interrupted]")
        return 130
    except Exception as exc:
        _print(f"error: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
