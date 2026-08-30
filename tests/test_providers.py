"""Provider registry tests: every major player resolvable, headers/auth wired correctly."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from saturday.config import PROVIDERS, AgentConfig  # noqa: E402
from saturday.llm.providers import build_client  # noqa: E402


@pytest.fixture(autouse=True)
def _hermetic_user_config(monkeypatch, tmp_path):
    """Isolate tests from the user's real ~/.saturday/config.json and SATURDAY_* env."""
    from saturday import config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_FILE", tmp_path / "config.json")
    for k in [k for k in os.environ if k.startswith("SATURDAY_")]:
        monkeypatch.delenv(k)

MAJOR_PROVIDERS = [
    "deepseek",
    "openai",
    "openrouter",
    "ollama",
    "vllm",
    "anthropic",
    "google",
    "nous",
    "xai",
    "mistral",
    "groq",
    "moonshot",
    "qwen",
    "zai",
    "azure-openai",
    "together",
]


def test_all_major_providers_registered():
    missing = [p for p in MAJOR_PROVIDERS if p not in PROVIDERS]
    assert not missing, f"missing providers: {missing}"
    for name, prof in PROVIDERS.items():
        assert prof.name == name
        if prof.base_url:
            assert prof.base_url.startswith(("http://", "https://")), f"{name} base_url invalid"
        else:
            assert prof.api_key_env == "AZURE_OPENAI_API_KEY", "only azure may ship an empty base_url"
        assert prof.api_key_env.endswith("_API_KEY")
        assert prof.default_model


@pytest.mark.parametrize(
    "provider,env,model",
    [
        ("anthropic", "ANTHROPIC_API_KEY", None),
        ("google", "GEMINI_API_KEY", None),
        ("nous", "NOUS_API_KEY", None),
        ("xai", "XAI_API_KEY", None),
        ("moonshot", "MOONSHOT_API_KEY", None),
        ("qwen", "DASHSCOPE_API_KEY", None),
        ("zai", "ZAI_API_KEY", None),
        ("mistral", "MISTRAL_API_KEY", None),
        ("groq", "GROQ_API_KEY", None),
        ("together", "TOGETHER_API_KEY", None),
        ("azure-openai", "AZURE_OPENAI_API_KEY", "my-deployment"),
    ],
)
def test_profile_resolves_model_and_key(provider, env, model, monkeypatch):
    monkeypatch.setenv(env, "test-key-123")
    overrides = {"provider": provider}
    if model:
        overrides["model"] = model
    cfg = AgentConfig.load(overrides)
    prof = cfg.profile()
    assert prof.resolve_api_key() == "test-key-123"
    expected_default = {
        "anthropic": "claude-opus-5",
        "google": "gemini-3.7-flash",
        "nous": "Hermes-4-70B",
        "xai": "grok-4.6",
        "moonshot": "kimi-k2.5",
        "qwen": "qwen3.8-max",
        "zai": "glm-5.3",
        "mistral": "mistral-large-latest",
        "groq": "llama-3.3-70b-versatile",
        "together": "Qwen/Qwen3.8-27B",
    }.get(provider)
    if expected_default and not model:
        assert cfg.model == expected_default


def test_anthropic_uses_bearer_via_openai_compat_layer(monkeypatch):
    """Per Anthropic docs, the OpenAI-compatible /v1/chat/completions layer
    takes the key as Authorization: Bearer — the native x-api-key and
    anthropic-version headers are /v1/messages-only and must not be sent."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    cfg = AgentConfig(provider="anthropic")
    client = build_client(cfg)
    assert client.api_key == "sk-ant-test"
    assert client.extra_headers.get("x-api-key") is None, "native header on the compat layer"
    assert client.extra_headers.get("anthropic-version") is None


def test_azure_uses_api_key_header_not_bearer(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-key")
    monkeypatch.setenv("AZURE_OPENAI_BASE_URL", "https://myres.openai.azure.com")
    cfg = AgentConfig(provider="azure-openai")
    client = build_client(cfg)
    assert client.extra_headers.get("api-key") == "azure-key"
    assert client.api_key == "", "azure must not receive a Bearer api key"
    assert client.deployment_path is True


def test_azure_endpoint_url_is_deployment_path_with_api_version(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_BASE_URL", "https://myres.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_MODEL", "my-deployment")
    client = build_client(AgentConfig(provider="azure-openai"))
    assert client._endpoint_url() == (
        "https://myres.openai.azure.com/openai/deployments/my-deployment/"
        "chat/completions?api-version=2024-10-21"
    )


def test_azure_fails_loudly_without_base_url(monkeypatch):
    monkeypatch.delenv("AZURE_OPENAI_BASE_URL", raising=False)
    with pytest.raises(ValueError, match="AZURE_OPENAI_BASE_URL"):
        build_client(AgentConfig(provider="azure-openai"))


def test_deepseek_uses_doc_sampling_defaults(monkeypatch):
    """DeepSeek docs: deepseek-reasoner wants temperature=1.0/top_p=1.0."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    client = build_client(AgentConfig(provider="deepseek"))
    assert client.sample_defaults == {"temperature": 1.0, "top_p": 1.0}
    captured = {}

    def fake_post(payload, body=None, model=None):
        captured["payload"] = payload
        return {"choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}], "usage": {}}

    client._chat_once = fake_post
    client.chat([{"role": "user", "content": "x"}])
    assert captured["payload"]["temperature"] == 1.0
    assert captured["payload"]["top_p"] == 1.0


def test_google_omits_sampling_params(monkeypatch):
    """Gemini docs: temperature/top_p deprecated — must not be sent."""
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    client = build_client(AgentConfig(provider="google"))
    assert client.omit_sampling is True
    captured = {}

    def fake_post(payload, body=None, model=None):
        captured["payload"] = payload
        return {"choices": [{"message": {"role": "assistant", "content": "hi"}}], "usage": {}}

    client._chat_once = fake_post
    client.chat([{"role": "user", "content": "x"}])
    assert "temperature" not in captured["payload"]
    assert "top_p" not in captured["payload"]


def test_openrouter_sends_attribution_headers_and_bearer(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    client = build_client(AgentConfig(provider="openrouter"))
    assert client.extra_headers.get("HTTP-Referer") == "https://github.com/MershLab/saturday"
    assert client.extra_headers.get("X-Title") == "Saturday"
    assert client.api_key == "or-key"


def test_parse_reasoning_details_and_refusal():
    from saturday.types import Message

    m = Message.from_openai({
        "content": "answer",
        "reasoning_details": [
            {"type": "reasoning.text", "text": "step one"},
            {"type": "reasoning.summary", "text": "step two"},
        ],
    })
    assert m.reasoning == "step one\nstep two"
    m2 = Message.from_openai({"content": None, "refusal": "I can't do that."})
    assert m2.content == "I can't do that."


def test_user_extra_headers_override_profile(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    cfg = AgentConfig(provider="anthropic", extra_headers={"anthropic-version": "2023-06-01-x"})
    client = build_client(cfg)
    assert client.extra_headers["anthropic-version"] == "2023-06-01-x"


def test_probe_azure_models_url_and_anthropic_bearer(monkeypatch):
    from saturday.llm.probe import probe_connection, probe_headers

    captured = {}

    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return b'{"data": [{"id": "my-deployment"}]}'

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = req.headers
        return R()

    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_BASE_URL", "https://myres.openai.azure.com")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    ok, detail, models = probe_connection(PROVIDERS["azure-openai"], api_key="k")
    assert ok and models == ["my-deployment"]
    assert "/openai/models?api-version=2024-10-21" in captured["url"]
    assert captured["headers"].get("Api-key") == "k"
    assert probe_headers(PROVIDERS["anthropic"], "sk-ant-test").get("Authorization") == "Bearer sk-ant-test"
    assert "x-api-key" not in probe_headers(PROVIDERS["anthropic"], "sk-ant-test")


def test_unknown_provider_lists_available():
    with pytest.raises(ValueError) as excinfo:
        AgentConfig(provider="nonexistent").profile()
    assert "anthropic" in str(excinfo.value) and "google" in str(excinfo.value)


def test_cli_provider_choices_cover_majors():
    from saturday.config import PROVIDERS as P

    for p in ("anthropic", "google", "nous", "xai"):
        assert p in P
