"""Live smoke test against a real provider. Skips honestly without credentials."""
from __future__ import annotations


from saturday.agent import Agent
from saturday.config import AgentConfig
from saturday.utils.env import load_env_file


def main() -> int:
    load_env_file()
    cfg = AgentConfig.load()
    profile = cfg.profile()
    if profile.name not in ("ollama",) and not profile.resolve_api_key():
        print(f"SKIP: set {profile.api_key_env} to run the live smoke test.")
        return 0

    agent = Agent(cfg=cfg)
    traj = agent.run(
        "Use the python tool to compute 12345*67 and reply with just the number.",
        on_text_delta=lambda s: print(s, end="", flush=True),
        on_reasoning_delta=lambda s: print(s, end="", flush=True),
    )
    ok = "827115" in (traj.final_answer or "")
    print(f"\n\nstop={traj.stop_reason} steps={len(traj.steps)} tokens={traj.usage.total_tokens}")
    print("SMOKE PASS" if ok else "SMOKE FAIL: expected 827115 in answer")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
