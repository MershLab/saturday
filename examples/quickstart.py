"""Live quickstart: point at any OpenAI-compatible provider and run a task."""
from __future__ import annotations

from saturday.agent import Agent
from saturday.config import AgentConfig


def main() -> int:
    cfg = AgentConfig.load(
        {
            "provider": "deepseek",
            "model": "deepseek-reasoner",
            "max_steps": 30,
        }
    )
    agent = Agent(cfg=cfg)
    traj = agent.run(
        "Inspect this repository, then write a one-paragraph summary of its architecture "
        "into ARCHITECTURE.md. Verify the file exists before finishing.",
        on_reasoning_delta=lambda s: print(s, end="", flush=True),
        on_text_delta=lambda s: print(s, end="", flush=True),
    )
    print(f"\n\n---\n{traj.final_answer}")
    print(f"[stop={traj.stop_reason} steps={len(traj.steps)} tokens={traj.usage.total_tokens}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
