"""Connection probe + model discovery shared by every setup surface.

One GET to {base_url}/models validates the key and endpoint and returns the
provider's model list (when it publishes one) — no tokens spent. Used by the
web onboarding wizard, the Settings "test" button and `saturday setup`.
"""
from __future__ import annotations

import json
import urllib.request


def probe_headers(profile, api_key: str) -> dict[str, str]:
    """Auth headers for a probe, honoring each provider's scheme."""
    if profile.api_key_header:  # azure-openai: docs use the api-key header
        return {profile.api_key_header: api_key} if api_key else {}
    # anthropic via its OpenAI-compatible layer and everything else: Bearer
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _parse_models(raw: bytes) -> list[str]:
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return []
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    ids: list[str] = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            if item["id"] not in ids:
                ids.append(item["id"])
    return ids[:500]


def probe_connection(profile, api_key: str = "", timeout: float = 8.0) -> tuple[bool, str, list[str]]:
    """Returns (ok, human_detail, models). Never raises.

    A 401/403 means the endpoint answered but rejected the key — the most
    useful signal when onboarding. An empty model list is not a failure:
    some servers serve /models without publishing ids.
    """
    base = profile.resolve_base_url().rstrip("/")
    if not base:
        return False, f"endpoint not configured (set {profile.base_url_env})", []
    if getattr(profile, "deployment_path", False):
        # Azure docs: model list lives at /openai/models?api-version=…
        if base.endswith("/models"):
            url = base
        else:
            url = base + "/openai/models"
        ver = profile.resolve_api_version()
        if ver and "api-version=" not in url:
            sep = "&" if "?" in url else "?"
            url += f"{sep}api-version={ver}"
    else:
        url = base + "/models"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, method="GET", headers=probe_headers(profile, api_key)),
            timeout=timeout,
        ) as resp:
            raw = resp.read(1024 * 1024)
    except Exception as exc:
        code = getattr(exc, "code", None)
        if code in (401, 403):
            return False, "auth rejected — check the API key", []
        if code is not None:
            return False, f"endpoint answered with HTTP {code}", []
        return False, f"endpoint unreachable ({type(exc).__name__})", []
    models = _parse_models(raw)
    if models:
        return True, f"reachable — {len(models)} models found", models
    return True, "reachable", []
