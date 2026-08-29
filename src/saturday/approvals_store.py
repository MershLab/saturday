"""Persistent approval rules (hermes command_allowlist parity).

Rules live in ``CONFIG_DIR/approvals.json`` and feed safety.check_command.
Allow rules only remove repeat ASKS (hardline, deny mode, reserved tiers and
guardrails still apply); deny rules are enforced unconditionally — in every
mode including safety=off.

Stdlib-only."""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

_LOCK = threading.Lock()


def _rules_path() -> Path:
    from saturday.config import get_config_dir

    return Path(get_config_dir()) / "approvals.json"


def load_rules() -> dict[str, list[str]]:
    try:
        data = json.loads(_rules_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"allow": [], "deny": []}
    if not isinstance(data, dict):
        return {"allow": [], "deny": []}
    out: dict[str, list[str]] = {"allow": [], "deny": []}
    for kind in ("allow", "deny"):
        raw = data.get(kind)
        if isinstance(raw, list):
            seen: list[str] = []
            for r in raw[:200]:
                if isinstance(r, str) and r.strip() and r.strip() not in seen:
                    seen.append(r.strip())
            out[kind] = seen
    return out


def add_rule(kind: str, rule: str) -> None:
    """kind: allow | deny. Rules are exact normalized commands or 'prefix*'."""
    if kind not in ("allow", "deny"):
        raise ValueError(f"bad rule kind '{kind}'")
    rule = str(rule or "").strip()
    if not rule or len(rule) > 500 or "\n" in rule:
        raise ValueError("rule must be a single line of at most 500 characters")
    with _LOCK:
        rules = load_rules()
        if rule not in rules[kind]:
            rules[kind].append(rule)
        _rules_path().parent.mkdir(parents=True, exist_ok=True)
        _rules_path().write_text(json.dumps(rules, indent=2), encoding="utf-8")


def remove_rule(kind: str, rule: str) -> bool:
    if kind not in ("allow", "deny"):
        raise ValueError(f"bad rule kind '{kind}'")
    with _LOCK:
        rules = load_rules()
        before = len(rules[kind])
        rules[kind] = [r for r in rules[kind] if r != rule.strip()]
        changed = len(rules[kind]) < before
        if changed:
            _rules_path().parent.mkdir(parents=True, exist_ok=True)
            _rules_path().write_text(json.dumps(rules, indent=2), encoding="utf-8")
        return changed


def clear_rules(kind: str | None = None) -> None:
    """Wipe one list (kind='allow' | 'deny') or both when kind is None."""
    if kind is not None and kind not in ("allow", "deny"):
        raise ValueError(f"bad rule kind '{kind}'")
    with _LOCK:
        rules = load_rules()
        for k in ([kind] if kind else ("allow", "deny")):
            rules[k] = []
        # fresh install: the config dir may not exist yet, write_text would
        # raise FileNotFoundError without this
        _rules_path().parent.mkdir(parents=True, exist_ok=True)
        _rules_path().write_text(json.dumps(rules, indent=2), encoding="utf-8")


def shell_operators_present(text: str) -> bool:
    return bool(re.search(r"&&|\|\||;|\||`|\$\(", text))
