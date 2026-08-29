"""Offline end-to-end demo: the full Saturday harness with a scripted model.

Runs think -> act -> observe cycles, todo planning, goal tracking, and
verifiable-reward scoring without any API key.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from fakes import make_scripted_model

from saturday.agent.core import Agent
from saturday.config import AgentConfig
from saturday.eval.runner import EvalCase, EvalRunner, composite, file_created


class ScriptedAgent:
    def __init__(self, answer: str) -> None:
        self.answer = answer

    def run(self, task: str):
        from saturday.types import Trajectory

        return Trajectory(task=task, system_prompt="s", final_answer=self.answer, stop_reason="done")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="saturday_demo_"))
    cfg = AgentConfig(provider="vllm", workspace_root=str(tmp), max_steps=8)

    class OfflineProfile:
        name = "offline"

    cfg.profile = lambda: OfflineProfile()

    agent = Agent(cfg=cfg)

    scripted = make_scripted_model(
        [
            {
                "reasoning": (
                    "Goal is a file with today's note. Unknowns: none. "
                    "Best action: write_file. Expect: ok confirmation."
                ),
                "tool_calls": [
                    {"name": "todo", "arguments": {"action": "write", "steps_text": "write note\nverify content"}},
                    {"name": "create_goal", "arguments": {"text": "leave a note on disk"}},
                ],
            },
            {
                "reasoning": "Plan recorded and goal active. Next: write the file.",
                "tool_calls": [{"name": "write_file", "arguments": {"path": "note.txt", "content": "Saturday demo"}}],
            },
            {
                "reasoning": "Verify by reading back; also mark step 1 done.",
                "tool_calls": [
                    {"name": "read_file", "arguments": {"path": "note.txt"}},
                    {"name": "todo", "arguments": {"action": "mark", "index": 1}},
                ],
            },
            {
                "reasoning": "Content verified. Close out plan and goal, then answer.",
                "tool_calls": [
                    {"name": "todo", "arguments": {"action": "mark", "index": 2}},
                    {"name": "update_goal", "arguments": {"action": "complete"}},
                    {"name": "get_goal", "arguments": {}},
                ],
            },
            {
                "reasoning": "All steps verified; produce final summary.",
                "content": "Wrote and verified note.txt containing 'Saturday demo'. Goal completed.",
            },
        ]
    )

    agent._ensure_client = lambda: scripted  # inject offline model

    print("=== task ===")
    traj = agent.run("Create note.txt containing 'Saturday demo' and verify it.")
    for step in traj.steps:
        if step.assistant.reasoning:
            print(f"[think {step.index}] {step.assistant.reasoning[:90]}...")
        for r in step.results:
            mark = "+" if r.ok else "x"
            print(f"  [{mark}] {r.name}")
    print("\n=== final ===")
    print(traj.final_answer)
    print(f"stop={traj.stop_reason} steps={len(traj.steps)} tokens={traj.usage.total_tokens}")

    print("\n=== eval (verifiable reward) ===")
    runner = EvalRunner(lambda: ScriptedAgent("note.txt exists with Saturday demo"), out_dir=str(tmp / "eval"))
    results = runner.run(
        [
            EvalCase(
                id="note_check",
                task="create note.txt",
                verifier=composite(file_created(str(tmp / "note.txt"), must_contain=("Saturday demo",))),
            )
        ]
    )
    print(EvalRunner.summarize(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
