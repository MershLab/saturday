from __future__ import annotations

from saturday.config import PROVIDERS, AgentConfig
from saturday.llm.client import LLMClient


def build_client(cfg: AgentConfig) -> LLMClient:
    profile = cfg.profile()
    base_url = profile.resolve_base_url()
    if profile.deployment_path and not base_url:
        # docs: Azure has no default endpoint — fail with the env var name
        # instead of sending requests to a relative URL
        raise ValueError(
            f"{profile.base_url_env} is required for '{profile.name}' "
            "(Azure docs: https://<resource>.openai.azure.com)"
        )
    api_key = profile.resolve_api_key()
    headers: dict[str, str] = {}
    if profile.api_key_header and api_key:
        headers[profile.api_key_header] = api_key
        api_key = ""
    headers.update(profile.resolve_headers())
    headers.update(cfg.extra_headers or {})
    sample_defaults: dict[str, float] = {}
    if profile.sample_temperature is not None:
        sample_defaults["temperature"] = profile.sample_temperature
    if profile.sample_top_p is not None:
        sample_defaults["top_p"] = profile.sample_top_p
    return LLMClient(
        base_url=base_url,
        api_key=api_key,
        model=cfg.model or profile.resolve_default_model(),
        timeout=cfg.request_timeout,
        max_retries=cfg.max_retries,
        extra_headers=headers or None,
        fallback_models=getattr(cfg, "fallback_models", []) or [],
        overflow_markers=tuple(getattr(profile, "overflow_markers", ()) or ()),
        deployment_path=getattr(profile, "deployment_path", False),
        api_version=profile.resolve_api_version(),
        sample_defaults=sample_defaults or None,
        omit_sampling=getattr(profile, "omit_sampling", False),
    )


__all__ = ["build_client", "PROVIDERS"]
