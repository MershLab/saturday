"""Hermes parity: SOUL.md identity block rides into the system prompt.

AGENTS.md project instructions are owned by Agent._rules_block (precedence
contract in test_competitive_parity) — SOUL.md must stay out of that path.
"""
from __future__ import annotations

from saturday.agent.core import Agent
from saturday.config import AgentConfig, load_soul


def test_load_soul_reads_global_file(tmp_path, monkeypatch):
    import saturday.config as cfg

    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path / "home")
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    (tmp_path / "home" / "SOUL.md").write_text("You are Sat, a calm operator.", encoding="utf-8")
    assert "calm operator" in load_soul()


def test_soul_flows_into_agent_persona(tmp_path, monkeypatch):
    import saturday.config as cfg

    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path / "home")
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    (tmp_path / "home" / "SOUL.md").write_text("Identity: keep answers under 80 words.", encoding="utf-8")
    cfg0 = AgentConfig(workspace_root=str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    agent = Agent(cfg=cfg0, plugins=[])
    assert "keep answers under 80 words" in agent.persona_extra
    assert "# SOUL (identity)" in agent.persona_extra


def test_missing_soul_is_fine(tmp_path, monkeypatch):
    import saturday.config as cfg

    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path / "home")
    cfg0 = AgentConfig(workspace_root=str(tmp_path / "nope"))
    agent = Agent(cfg=cfg0, plugins=[])
    assert agent.persona_extra == ""
