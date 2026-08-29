"""Global test hermeticity: every test runs against an isolated SATURDAY home.

History: multiple sessions leaked test artifacts into the USER'S REAL
~/.saturday (873 junk session files, 30 junk projects, 371 usage rows — all
quarantined/cleaned 2026-08-25). Root cause: any test constructing
SessionStore()/ProjectStore()/AppState()/AgentConfig.load() without explicit
isolation silently used the real CONFIG_DIR.

This autouse fixture redirects the config home for EVERY test before anything
else runs. A test-specific fixture that patches CONFIG_DIR/CONFIG_FILE itself
(e.g. the browser e2e fixtures) applies after this one and wins — both styles
compose because each uses its own monkeypatch instance.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _hermetic_saturday_home(tmp_path, monkeypatch):
    import saturday.config as cfgmod

    home = tmp_path / "dfhome"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SATURDAY_HOME", str(home))
    # CONFIG_FILE=None => derived from CONFIG_DIR at call time (get_config_file),
    # so patching the dir alone propagates everywhere.
    monkeypatch.setattr(cfgmod, "CONFIG_DIR", home)
    monkeypatch.setattr(cfgmod, "CONFIG_FILE", None)
    yield
