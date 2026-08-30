from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Root of all Saturday local state. Tests may monkeypatch CONFIG_DIR; every
# reader must go through get_config_dir()/get_config_file() so a patched dir
# propagates (the old import-time CONFIG_FILE binding was the root cause of
# repeated test-isolation leaks: patching CONFIG_DIR alone silently kept the
# real user's config file in play).
CONFIG_DIR = Path(os.environ.get("SATURDAY_HOME", Path.home() / ".saturday"))
CONFIG_FILE: Path | None = None  # explicit override; None => derive from CONFIG_DIR


def get_config_dir() -> Path:
    return Path(CONFIG_DIR)


def get_config_file() -> Path:
    if CONFIG_FILE is not None:
        return Path(CONFIG_FILE)
    return Path(CONFIG_DIR) / "config.json"


def load_soul() -> str:
    """User-level identity block (Hermes SOUL.md parity).

    ~/.saturday/SOUL.md shapes the agent across every session. Project
    instructions are handled separately by Agent._rules_block (AGENTS.md /
    CLAUDE.md with workspace precedence) — this function must NOT load them,
    or that precedence contract breaks.
    """
    soul = get_config_dir() / "SOUL.md"
    if not soul.is_file():
        return ""
    try:
        body = soul.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return "# SOUL (identity)\n" + body if body else ""


@dataclass
class ProviderProfile:
    name: str
    base_url: str
    api_key_env: str
    default_model: str
    supports_reasoning: bool = False
    reasoning_style: str = "none"
    profile_headers: dict[str, str] = field(default_factory=dict)
    api_key_header: str = ""
    base_url_env: str = ""
    model_env: str = ""
    # Provider-specific context-overflow body markers (matched in addition to
    # the generic phrases): 16 backends phrase "prompt too long" differently.
    overflow_markers: tuple[str, ...] = ()
    # Azure-style routing: the chat URL is NOT {base}/chat/completions but
    # {base}/openai/deployments/{model}/chat/completions?api-version=..., and
    # the "model" field is omitted from the body (deployment is in the URL).
    deployment_path: bool = False
    api_version_env: str = ""
    api_version: str = ""
    # Doc-recommended sampling values (e.g. DeepSeek: reasoning models want
    # temperature=1.0/top_p=1.0 — a lower value errors or degrades thinking).
    sample_temperature: float | None = None
    sample_top_p: float | None = None
    # Docs removed the sampling params entirely (Gemini deprecated
    # temperature/top_p/top_k 2026-07): never send them for this provider.
    omit_sampling: bool = False

    def resolve_api_key(self) -> str:
        return os.environ.get(self.api_key_env, "")

    def resolve_base_url(self) -> str:
        if self.base_url_env and os.environ.get(self.base_url_env):
            return os.environ[self.base_url_env]
        return self.base_url

    def resolve_default_model(self) -> str:
        if self.model_env and os.environ.get(self.model_env):
            return os.environ[self.model_env]
        return self.default_model

    def resolve_api_version(self) -> str:
        """Azure: api-version=YYYY-MM-DD is a required query param per docs."""
        if self.api_version_env and os.environ.get(self.api_version_env):
            return os.environ[self.api_version_env]
        return self.api_version

    def resolve_headers(self) -> dict[str, str]:
        key = self.resolve_api_key()
        return {k: v.replace("{api_key}", key) for k, v in self.profile_headers.items()}


PROVIDERS: dict[str, ProviderProfile] = {
    "deepseek": ProviderProfile(
        name="deepseek",
        base_url="https://api.deepseek.com/v1",
        api_key_env="DEEPSEEK_API_KEY",
        default_model="deepseek-reasoner",
        supports_reasoning=True,
        reasoning_style="deepseek",
        # docs: deepseek-reasoner expects temperature=1.0/top_p=1.0 —
        # values below are ignored, zero can disable thinking entirely
        sample_temperature=1.0,
        sample_top_p=1.0,
    ),
    "openai": ProviderProfile(
        name="openai",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        default_model="gpt-4o-mini",
        overflow_markers=("context_length_exceeded",),
    ),
    "openrouter": ProviderProfile(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        default_model="deepseek/deepseek-r1",
        supports_reasoning=True,
        reasoning_style="deepseek",
        # OpenRouter app attribution (docs): HTTP-Referer identifies the app
        # for usage pages/rankings; X-Title sets its display name.
        profile_headers={
            "HTTP-Referer": "https://github.com/MershLab/saturday",
            "X-Title": "Saturday",
        },
    ),
    "ollama": ProviderProfile(
        name="ollama",
        base_url="http://localhost:11434/v1",
        api_key_env="OLLAMA_API_KEY",
        default_model="hermes3",
    ),
    "vllm": ProviderProfile(
        name="vllm",
        base_url="http://localhost:8000/v1",
        base_url_env="VLLM_BASE_URL",
        api_key_env="VLLM_API_KEY",
        default_model="deepseek-ai/DeepSeek-R1-Distill-Qwen-32B",
        model_env="VLLM_MODEL",
        supports_reasoning=True,
        reasoning_style="deepseek",
    ),
    "anthropic": ProviderProfile(
        name="anthropic",
        base_url="https://api.anthropic.com/v1/",
        base_url_env="ANTHROPIC_BASE_URL",
        api_key_env="ANTHROPIC_API_KEY",
        default_model="claude-opus-5",
        model_env="ANTHROPIC_MODEL",
        overflow_markers=("prompt is too long", "input length and"),
        # per docs, the OpenAI-compatible /v1/chat/completions layer takes the
        # key as Authorization: Bearer — the native x-api-key/anthropic-version
        # headers belong to /v1/messages only and are not sent here
    ),
    "google": ProviderProfile(
        name="google",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        base_url_env="GEMINI_BASE_URL",
        api_key_env="GEMINI_API_KEY",
        default_model="gemini-3.7-flash",
        # docs: temperature/top_p/top_k deprecated for gemini-3.7-flash —
        # sending them can 400; omit so the API applies its own defaults
        omit_sampling=True,
        model_env="GEMINI_MODEL",
        overflow_markers=("exceeds the maximum number of tokens", "input token limit"),
    ),
    "nous": ProviderProfile(
        name="nous",
        base_url="https://inference-api.nousresearch.com/v1",
        base_url_env="NOUS_BASE_URL",
        api_key_env="NOUS_API_KEY",
        default_model="Hermes-4-70B",
        model_env="NOUS_MODEL",
        supports_reasoning=True,
        reasoning_style="hermes",
    ),
    "xai": ProviderProfile(
        name="xai",
        base_url="https://api.x.ai/v1",
        base_url_env="XAI_BASE_URL",
        api_key_env="XAI_API_KEY",
        default_model="grok-4.6",
        model_env="XAI_MODEL",
    ),
    "mistral": ProviderProfile(
        name="mistral",
        base_url="https://api.mistral.ai/v1",
        base_url_env="MISTRAL_BASE_URL",
        api_key_env="MISTRAL_API_KEY",
        default_model="mistral-large-latest",
        model_env="MISTRAL_MODEL",
    ),
    "groq": ProviderProfile(
        name="groq",
        base_url="https://api.groq.com/openai/v1",
        base_url_env="GROQ_BASE_URL",
        api_key_env="GROQ_API_KEY",
        default_model="llama-3.3-70b-versatile",
        model_env="GROQ_MODEL",
    ),
    "moonshot": ProviderProfile(
        name="moonshot",
        base_url="https://api.moonshot.ai/v1",
        base_url_env="MOONSHOT_BASE_URL",
        api_key_env="MOONSHOT_API_KEY",
        default_model="kimi-k2.5",
        model_env="MOONSHOT_MODEL",
    ),
    "qwen": ProviderProfile(
        name="qwen",
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        base_url_env="DASHSCOPE_BASE_URL",
        api_key_env="DASHSCOPE_API_KEY",
        default_model="qwen3.8-max",
        model_env="QWEN_MODEL",
    ),
    "zai": ProviderProfile(
        name="zai",
        base_url="https://api.z.ai/api/paas/v4",
        base_url_env="ZAI_BASE_URL",
        api_key_env="ZAI_API_KEY",
        default_model="glm-5.3",
        model_env="ZAI_MODEL",
    ),
    "azure-openai": ProviderProfile(
        name="azure-openai",
        base_url="",
        base_url_env="AZURE_OPENAI_BASE_URL",
        api_key_env="AZURE_OPENAI_API_KEY",
        default_model="gpt-5.6-sol",
        model_env="AZURE_OPENAI_MODEL",
        api_key_header="api-key",
        # docs: {endpoint}/openai/deployments/{deployment}/chat/completions
        # ?api-version=YYYY-MM-DD with api-key header; model field omitted
        deployment_path=True,
        api_version_env="AZURE_OPENAI_API_VERSION",
        api_version="2024-10-21",
        overflow_markers=("context_length_exceeded",),
    ),
    "together": ProviderProfile(
        name="together",
        base_url="https://api.together.xyz/v1",
        base_url_env="TOGETHER_BASE_URL",
        api_key_env="TOGETHER_API_KEY",
        default_model="Qwen/Qwen3.8-27B",
        model_env="TOGETHER_MODEL",
    ),
}


@dataclass
class AgentConfig:
    provider: str = "deepseek"
    model: str | None = None
    temperature: float = 0.6
    top_p: float = 0.95
    max_tokens: int = 8192
    # Long-horizon coding tasks (multi-file refactors, benchmark runs) need far
    # more than the old 40-turn budget; 200 matches the web UI's upper bound.
    max_steps: int = 200
    max_context_tokens: int | None = None
    # Compaction threshold. None = AUTO: derived per model as 70% of its
    # resolved context window (the old fixed 60_000 default compacted at 6%
    # on 1M-token models). An explicit number wins, capped at 90% of window.
    compact_above_tokens: int | None = None
    request_timeout: float = 300.0
    max_retries: int = 4
    tools: list[str] = field(default_factory=list)
    # dsh parity: builds/compilers routinely exceed 60s; the watchdog exists to
    # catch HUNG tools, not to kill slow-but-working ones.
    tool_timeout: float = 120.0
    shell_allow_network: bool = True
    workspace_root: str = field(default_factory=lambda: os.getcwd())
    memory_max_chars: int = 12_000
    stream: bool = True
    fallback_models: list[str] = field(default_factory=list)
    safety_mode: str = "ask"
    keep_reasoning_in_history: bool = False
    # Zed/OpenHands/Goose parity: rename fresh sessions with a model-generated
    # title after the first completed turn (one tiny background call)
    auto_title_sessions: bool = True
    # Devin/Cursor parity: after each completed turn, offer model-generated
    # follow-up prompts as one-click chips above the composer
    suggest_followups: bool = True
    mcp_servers: dict[str, dict] = field(default_factory=dict)
    mcp_warnings: list[str] = field(default_factory=list)
    extra_headers: dict[str, str] = field(default_factory=dict)
    desktop_background_only: bool = False
    persona_extra: str = ""
    auth_scopes: dict[str, list[str]] = field(default_factory=dict)
    persona_mode: str = "agent"
    destructive_guardrails: bool = True
    # Structural isolation flag (container/job-object executor): replaces
    # pattern-based friction with a real boundary. Hardline blocks, deny rules
    # and reserved scopes still apply; guardrail/dangerous asks are skipped.
    sandboxed: bool = False
    # Plan mode (Cursor/Cline/Roo parity): restrict the agent to read-only
    # tools and require explicit approval before any mutation.
    plan_mode: bool = False
    # Hard spend policy: abort the run when cumulative tokens exceed this
    # (0 = off). Mirrors Omnigent-style spend enforcement.
    max_run_tokens: int = 0
    # Wall-clock cap for a single run (0 = off) - the one resource limit
    # that's genuinely cross-platform: real memory/CPU limits need OS
    # primitives that don't exist on every platform, this doesn't.
    max_wall_seconds: int = 0
    # Persist "always allow" decisions across sessions (CONFIG_DIR/approvals.json)
    persist_approvals: bool = True
    # Language servers for LSP tools, e.g. {"python": ["pylsp"]}
    lsp_servers: dict[str, list] = field(default_factory=dict)
    # Tool blocklist: exact tool names and/or family aliases ("web",
    # "computer_use", ... see TOOL_FAMILIES). Empty = everything enabled.
    disabled_tools: list[str] = field(default_factory=list)
    assistant_name: str = ""
    assistant_user_title: str = ""
    # Provenance marking on generated output (GB 45438-2025 / EU AI Act Art.50):
    # "metadata" stamps exports/bundles, "visible" adds a disclosure footer to
    # answers, "off" disables both.
    provenance_marking: str = "metadata"
    # Post-edit verification command (P1 roadmap): run after every successful
    # write_file/edit_file; "{path}" is substituted. Output surfaces inline so
    # the model self-corrects. Empty = off. SATURDAY_VERIFY_CMD overrides.
    verify_command: str = ""
    # Untrusted-content guard: tool output/web/screenshot text that matches
    # role-override/jailbreak patterns is withheld from the model. SATURDAY_INJECTION_GUARD=0 disables.
    injection_guard: bool = True
    # Hard-blocked app categories (Cowork parity): app_open target / window
    # query / ui window= matching one of these (case-insensitive substring)
    # is refused in every safety mode. SATURDAY_BLOCKED_APPS="a,b" overrides.
    blocked_apps: list[str] = field(default_factory=lambda: ["crypto", "trading", "wallet", "exchange"])

    @classmethod
    def load(cls, overrides: dict[str, Any] | None = None) -> "AgentConfig":
        data: dict[str, Any] = {}
        cfg_file = get_config_file()
        if cfg_file.exists():
            try:
                data = json.loads(cfg_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                # A silent reset here used to look like "Saturday forgot all
                # my settings"; surface WHY we fell back to defaults.
                print(f"[saturday] config unreadable, using defaults: {exc}", file=sys.stderr)
                data = {}
        env_map = {
            "provider": "SATURDAY_PROVIDER",
            "model": "SATURDAY_MODEL",
            "temperature": "SATURDAY_TEMPERATURE",
            "max_steps": "SATURDAY_MAX_STEPS",
            "workspace_root": "SATURDAY_WORKSPACE",
        }
        for key, env in env_map.items():
            if os.environ.get(env):
                raw = os.environ[env]
                if key in ("temperature",):
                    try:
                        data[key] = float(raw)
                    except ValueError:
                        pass  # malformed env value keeps the default
                elif key in ("max_steps",):
                    try:
                        data[key] = int(raw)
                    except ValueError:
                        pass  # malformed env value keeps the default
                else:
                    data[key] = raw
        if os.environ.get("SATURDAY_BACKGROUND_ONLY", ""):
            data["desktop_background_only"] = os.environ["SATURDAY_BACKGROUND_ONLY"].lower() in ("1", "true", "yes", "on")
        if os.environ.get("SATURDAY_GUARDRAILS", ""):
            data["destructive_guardrails"] = os.environ["SATURDAY_GUARDRAILS"].lower() in ("1", "true", "yes", "on")
        if os.environ.get("SATURDAY_SANDBOXED", ""):
            data["sandboxed"] = os.environ["SATURDAY_SANDBOXED"].lower() in ("1", "true", "yes", "on")
        if os.environ.get("SATURDAY_YOLO", ""):
            # fully-autonomous escape hatch, same contract as --yolo
            data["safety_mode"] = "autonomous"
        if os.environ.get("SATURDAY_VERIFY_CMD", ""):
            data["verify_command"] = os.environ["SATURDAY_VERIFY_CMD"]
        if os.environ.get("SATURDAY_PROVENANCE", ""):
            data["provenance_marking"] = os.environ["SATURDAY_PROVENANCE"].strip().lower()
        if os.environ.get("SATURDAY_INJECTION_GUARD", ""):
            data["injection_guard"] = os.environ["SATURDAY_INJECTION_GUARD"].lower() in ("1", "true", "yes", "on")
        if os.environ.get("SATURDAY_BLOCKED_APPS", ""):
            data["blocked_apps"] = [s.strip() for s in os.environ["SATURDAY_BLOCKED_APPS"].split(",") if s.strip()]
        if isinstance(data.get("safety_mode"), str):
            from saturday.safety import normalize_mode

            data["safety_mode"] = normalize_mode(data["safety_mode"])
        if overrides:
            data.update({k: v for k, v in overrides.items() if v is not None})
        if isinstance(data.get("disabled_tools"), str):
            data["disabled_tools"] = [s.strip() for s in data["disabled_tools"].split(",") if s.strip()]
        if isinstance(data.get("fallback_models"), str):
            data["fallback_models"] = [s.strip() for s in data["fallback_models"].split(",") if s.strip()]
        if data.get("provenance_marking") not in (None, "metadata", "visible", "off"):
            data["provenance_marking"] = "metadata"
        known = {f for f in cls.__dataclass_fields__}
        cfg = cls(**{k: v for k, v in data.items() if k in known})
        if cfg.model is None:
            prof = PROVIDERS.get(cfg.provider)
            cfg.model = prof.resolve_default_model() if prof else "deepseek-reasoner"
        try:
            from saturday.mcp_plugin import load_mcp_config

            mcp_problems: list[str] = []
            file_servers = load_mcp_config(warnings=mcp_problems)
            for alias, spec in file_servers.items():
                cfg.mcp_servers.setdefault(alias, spec)
            cfg.mcp_warnings.extend(mcp_problems)
        except Exception:
            pass
        return cfg

    def profile(self) -> ProviderProfile:
        prof = PROVIDERS.get(self.provider)
        if prof is None:
            raise ValueError(
                f"unknown provider '{self.provider}'. available: {', '.join(sorted(PROVIDERS))}"
                + _did_you_mean(self.provider, PROVIDERS)
            )
        return prof


def save_config(partial: dict[str, Any]) -> None:
    get_config_dir().mkdir(parents=True, exist_ok=True)
    cfg_file = get_config_file()
    existing: dict[str, Any] = {}
    if cfg_file.exists():
        try:
            existing = json.loads(cfg_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}
    existing.update(partial)
    # Atomic swap: a crash mid-write used to leave a truncated config.json,
    # which load() then silently reset to factory defaults. The temp file is
    # created in the same directory so os.replace stays a same-volume rename.
    tmp = cfg_file.with_name(f"{cfg_file.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        os.replace(tmp, cfg_file)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _did_you_mean(value: str, options: dict[str, Any]) -> str:
    """Suggest the closest known name for a typo'd provider (ease of use)."""
    v = str(value or "").strip().lower()
    if not v:
        return ""
    best, best_score = "", 0
    for name in options:
        common = sum(1 for a, b in zip(v, name) if a == b)
        score = common / max(len(v), len(name))
        if score > best_score:
            best, best_score = name, score
    return f" (did you mean '{best}'?)" if best and best_score >= 0.5 else ""
