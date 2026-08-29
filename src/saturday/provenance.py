"""Provenance marking for agent-generated content.

Regulatory context: China's GB 45438-2025 AI-labeling measures and EU AI Act
Art. 50 both require machine-readable provenance on AI-generated output. This
module stamps exported trajectories / audit bundles with a metadata block and
offers an optional visible footer for human-facing answers.

Modes (``AgentConfig.provenance_marking``):
  - ``"metadata"`` (default): provenance dict embedded in exports/bundles;
    answers untouched.
  - ``"visible"``: metadata + a short human-visible footer on final answers.
  - ``"off"``: nothing is added.

Stdlib-only, like the rest of the core.
"""
from __future__ import annotations

import hashlib
import time
from typing import Any

MARKING_MODES = ("metadata", "visible", "off")
GENERATOR = "Saturday"
FOOTER = "\n\n---\n*Generated with Saturday (AI-assisted content).*"


def content_fingerprint(record: dict[str, Any]) -> str:
    """Stable SHA-256 over the conversational payload being marked."""
    from saturday.sessions import canonical_json

    payload = {
        k: record.get(k)
        for k in ("task", "system", "messages", "final_answer")
        if record.get(k) is not None
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def provenance_block(
    *,
    provider: str = "",
    model: str = "",
    session_id: str = "",
    record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Machine-readable provenance fields (GB 45438-2025 style)."""
    return {
        "ai_generated": True,
        "generated_with": GENERATOR,
        "generator_version": _version(),
        "provider": provider or "",
        "model": model or "",
        "session_id": session_id or "",
        "ts": time.time(),
        "content_sha256": content_fingerprint(record or {}),
    }


def _version() -> str:
    try:
        from saturday import __version__

        return __version__
    except Exception:
        return "unknown"


def stamp_record(
    record: dict[str, Any],
    *,
    provider: str = "",
    model: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Return ``record`` with a ``provenance`` key added (never mutates input).

    The hash commits the pre-stamp content so downstream consumers can detect
    post-hoc edits to the exported conversation."""
    out = dict(record)
    out["provenance"] = provenance_block(
        provider=provider, model=model, session_id=session_id, record=record
    )
    return out


def visible_footer_enabled(marking: str | None) -> bool:
    return (marking or "metadata") == "visible"


def apply_visible_footer(answer: str, marking: str | None) -> str:
    """Append the disclosure footer when marking mode is 'visible'."""
    if not answer or not visible_footer_enabled(marking):
        return answer
    if "Generated with Saturday" in answer:
        return answer
    return answer + FOOTER
