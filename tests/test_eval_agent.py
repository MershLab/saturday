from __future__ import annotations

import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fakes import make_scripted_model  # noqa: E402

from saturday.agent.core import Agent  # noqa: E402
from saturday.config import AgentConfig  # noqa: E402
from saturday.eval.builtin import builtin_suite  # noqa: E402
from saturday.eval.runner import (  # noqa: E402
    EvalCase,
    EvalRunner,
    composite,
    contains_any,
    file_created,
    regex_matches,
)
from saturday.tasks import SubagentTask  # noqa: E402
from saturday.tools.base import ToolRegistry  # noqa: E402
from saturday.tools.files import WriteFile  # noqa: E402


def test_verifiers(tmp_path: Path):
    class T:
        final_answer = "The count is 25 exactly."
        task = "t"

    assert contains_any("25")(T()) == 1.0
    assert regex_matches(r"entries:\s*\d+")(types.SimpleNamespace(final_answer="entries: 7")) == 1.0
    assert regex_matches(r"entries:\s*\d+")(types.SimpleNamespace(final_answer="nope")) == 0.0
    f = tmp_path / "artifact.txt"
    f.write_text("payload", encoding="utf-8")
    v = composite(file_created(str(f), must_contain=("payload",)), contains_any("payload"))
    assert v(types.SimpleNamespace(final_answer="payload")) == 1.0


class ScriptedAgent:
    def __init__(self, answer: str) -> None:
        self.answer = answer

    def run(self, task: str):
        from saturday.types import Trajectory

        return Trajectory(task=task, system_prompt="s", final_answer=self.answer, stop_reason="done")


def test_eval_runner_saves_and_scores(tmp_path: Path):
    runner = EvalRunner(lambda: ScriptedAgent("answer: 25"), out_dir=str(tmp_path))
    cases = [
        EvalCase(id="c1", task="t1", verifier=contains_any("25")),
        EvalCase(id="c2", task="t2", verifier=regex_matches(r"\d+")),
    ]
    results = runner.run(cases)
    summary = EvalRunner.summarize(results)
    assert summary["cases"] == 2
    assert summary["mean_reward"] == 1.0
    assert summary["pass_rate"] == 1.0
    saved = json.loads((tmp_path / "c1.json").read_text(encoding="utf-8"))
    assert saved["reward"] == 1.0 and saved["task"] == "t1"


def test_subagent_tool_reports_and_errors():
    ok_tool = SubagentTask(runner=lambda prompt: "sub report done")
    ok, out = ok_tool.run({"prompt": "do it"})
    assert ok and out == "sub report done"
    bad = SubagentTask(runner=lambda prompt: (_ for _ in ()).throw(RuntimeError("boom")))
    ok, err = bad.run({"prompt": "x"})
    assert not ok and "boom" in err


def _offline_agent(tmp_path: Path) -> Agent:
    cfg = AgentConfig(provider="deepseek", model="deepseek-reasoner", workspace_root=str(tmp_path), max_steps=5)

    class StubProfile:
        name = "deepseek"

    cfg.profile = lambda: StubProfile()

    registry = ToolRegistry()
    registry.register(WriteFile(root=str(tmp_path)))

    agent = Agent(cfg=cfg, enable_subagents=False, registry=registry)
    return agent


def test_agent_facade_with_injected_client(tmp_path: Path, monkeypatch):
    import saturday.agent.core as core

    scripted = make_scripted_model(
        [
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "z.txt", "content": "ok"}}]},
            {"content": "wrote z.txt"},
        ]
    )

    def fake_build(self):
        self.client = scripted
        return scripted

    monkeypatch.setattr(core.Agent, "_ensure_client", fake_build)
    agent = _offline_agent(tmp_path)
    traj = agent.run("write z.txt containing ok")
    assert traj.stop_reason == "done"
    assert traj.final_answer == "wrote z.txt"
    assert (tmp_path / "z.txt").read_text() == "ok"


def test_builtin_suite_defined():
    cases = builtin_suite()
    ids = [c.id for c in cases]
    assert len(cases) >= 3 and len(ids) == len(set(ids))
