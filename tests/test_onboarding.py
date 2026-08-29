"""Onboarding plumbing: connection probe, model discovery, CLI first-run gate."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from saturday.config import PROVIDERS
from saturday.llm import probe as pr


class _Resp:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self, _n: int | None = None) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a) -> bool:
        return False


def _capture_urlopen(monkeypatch, err: Exception | None = None, data: bytes = b""):
    seen = {}

    def _open(req, timeout):  # noqa: ARG001
        if err is not None:
            raise err
        seen["req"] = req
        return _Resp(data)

    monkeypatch.setattr(urllib.request, "urlopen", _open, raising=True)
    return seen


def _models_payload(*ids: str) -> bytes:
    return json.dumps({"data": [{"id": i} for i in ids]}).encode()


def test_probe_ok_lists_models_deduplicated(monkeypatch):
    data = _models_payload("deepseek-r1", "deepseek-v3", "deepseek-r1")
    seen = _capture_urlopen(monkeypatch, data=data)
    ok, detail, models = pr.probe_connection(PROVIDERS["deepseek"], "k123")
    assert ok is True
    assert models == ["deepseek-r1", "deepseek-v3"]
    assert "2 models" in detail
    seen["req"].full_url.endswith("/models")
    assert seen["req"].headers["Authorization"] == "Bearer k123"


def test_probe_auth_rejected(monkeypatch):
    _capture_urlopen(monkeypatch, err=urllib.error.HTTPError("http://x", 401, "Unauthorized", {}, None))
    ok, detail, models = pr.probe_connection(PROVIDERS["deepseek"], "bad")
    assert ok is False
    assert "auth rejected" in detail
    assert models == []


def test_probe_unreachable(monkeypatch):
    _capture_urlopen(monkeypatch, err=urllib.error.URLError(OSError("boom")))
    ok, detail, _ = pr.probe_connection(PROVIDERS["deepseek"], "k123")
    assert ok is False
    assert "unreachable" in detail


def test_probe_garbage_ok_but_no_models(monkeypatch):
    _capture_urlopen(monkeypatch, data=b"<html>not json</html>")
    ok, _, models = pr.probe_connection(PROVIDERS["deepseek"], "k")
    assert ok is True
    assert models == []


def test_probe_azure_uses_api_key_header(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_BASE_URL", "http://azure.test/v1")
    seen = _capture_urlopen(monkeypatch, data=b"{}")
    ok, _, _ = pr.probe_connection(PROVIDERS["azure-openai"], "k9")
    assert ok is True
    # urllib capitalizes header keys: "api-key" -> "Api-key"
    assert seen["req"].headers.get("Api-key") == "k9"
    assert "Authorization" not in seen["req"].headers


def test_probe_anthropic_bearer_via_openai_compat(monkeypatch):
    """Anthropic's OpenAI-compatible layer takes the key as Bearer (docs);
    the native x-api-key/anthropic-version headers are for /v1/messages."""
    seen = _capture_urlopen(monkeypatch, data=b"{}")
    ok, _, _ = pr.probe_connection(PROVIDERS["anthropic"], "k9")
    assert ok is True
    assert seen["req"].headers.get("Authorization") == "Bearer k9"
    assert "X-api-key" not in seen["req"].headers
    assert "Anthropic-version" not in seen["req"].headers


def test_configured_or_hint_gates_missing_key(monkeypatch, tmp_path):
    import argparse

    from saturday import cli

    monkeypatch.setattr("saturday.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr("saturday.cli._print", lambda *a, **k: None)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    ns = argparse.Namespace(env=None)
    assert cli._configured_or_hint(ns) == 1

    monkeypatch.setenv("DEEPSEEK_API_KEY", "x")
    assert cli._configured_or_hint(ns) is None
