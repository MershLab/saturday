# Changelog

## Unreleased

## 0.9.0 — launch hardening, competitive-parity UI, test suite consolidation

### Fixed
- **False CPython 3.14 xfail in the safety regex canary test**: the xfail's stated
  reason was wrong, the canary pattern could never match on any Python version, not
  just 3.14. Replaced with real parametrized tests against the production
  `check_command()` dangerous-command detector; confirmed the real production
  pattern has no analogous defect.
- **Duplicate `tool_start` events**: two real concurrency bugs, `AppServer` was
  mutating shared class-level `Handler` state instead of per-instance state, and
  the event bus's subscribe/replay sequence had a window where a publish could be
  delivered twice. Fixed with a per-instance `Handler` subclass and an atomic
  `EventBus.subscribe_with_replay()`.
- **Keyboard text chunking on macOS/Linux**: `KeyboardTool` was shelling out to
  `xdotool` with the entire typed string as one unchunked argument, ignoring the
  injected runner entirely. Long text could hit the OS `ARG_MAX` limit in
  production. Now chunks consistently with the existing Windows path.
- **Wrong `saturdaylabs` org references**: the OpenRouter `HTTP-Referer`
  attribution header and the `pyproject.toml` authors field pointed at a
  nonexistent org, left over from before the move to MershLab.

### Internal
- **Test suite consolidated from 78 files to 8**, organized by subject
  (`test_safety`, `test_agent_core`, `test_providers`, `test_tools`,
  `test_webui`, `test_cli`, `test_scheduling`, `test_eval`), replacing a set of
  files named after development rounds/review passes rather than what they
  tested. Same test coverage preserved and verified by exact test ID across the
  reorg, `ruff check` clean.

### UI fix
- **Settings search box no longer changes shape between tabs**: the
  `.settings-body` grid used two auto-sized rows, so the default
  `align-content: stretch` poured leftover height from short panes
  (Schedules/MCP/About) into both rows and stretched the search input from
  26px to 77px. Explicit `grid-template-rows: auto 1fr` pins the search to
  its natural height, gives the leftover to the nav+panes row, and
  `align-self: start` on the input makes it immune to any future row change.
  Verified identical geometry across all 10 sections.

### Pre-launch security & wiring round (hardening)
- **Shell approval-rule smuggling closed (P1):** persistent allow/deny rules
  were matched against whitespace-folded command text, so a multiline command
  like `git status\nsudo …` could inherit a saved `git status*` allow rule and
  skip the dangerous-command ask (deny rules could be evaded the same way).
  Rule matching now operates on per-line probes of the raw command: multiline
  commands never inherit allow-rule suppression, deny rules match any
  contained line, and the approval dialog renders the raw multiline command
  instead of a folded one-line summary. Regressions in
  `tests/test_security_review_r2.py`.
- **Hardline floor binds in every mode (P1):** `safety=off` used to bypass the
  catastrophic blocklist (`rm -rf /`, `mkfs`, fork bombs) and GNU long flags
  (`rm --recursive --force /`) evaded the patterns entirely. The floor now
  binds in off mode too, long flags fold to short before scanning, and the
  root pattern was tightened to true root/system dirs — `rm -rf /tmp/cache`
  is normal cleanup and correctly lands in the guardrail-ask tier instead of
  being unconditionally blocked.
- **Token no longer accepted from URLs (P1):** `?k=` authenticated every
  endpoint, leaking the token into browser history and Referer headers. A
  one-shot cookie bootstrap on `GET /?k=` (Set-Cookie + `location.replace`)
  handles first load; everything else requires the header or cookie, and the
  frontend no longer appends tokens to `/api/file` image URLs.
- **Delete-all zombie race (P1):** wiping all sessions unlinked transcripts
  while busy runtimes could still append, resurrecting "deleted" sessions;
  the wipe now waits for busy runtimes to finish their final append.
- Hardening batch: malformed `Content-Length` returns 400 instead of killing
  the connection with a traceback; `/yolo` flips a per-agent safety override
  instead of mutating the shared base config (one chat's toggle no longer
  silences approvals for every concurrent session); `.env` upserts are
  `chmod 600` on POSIX; LLM-client redirects strip `Authorization`/
  `Cookie` when the host changes; the Telegram gateway answers a stranger
  chat exactly once (no unlimited liveness oracle).
- **Frontend wiring fixes:** `openSession` used `meta` before its `const`
  declaration — a TDZ ReferenceError that silently broke startup session
  restore, live re-attach to busy sessions, and edit-&-resend/branch. A
  dropped NDJSON stream no longer leaves the composer stuck in "working"
  (recover: re-check server state, re-attach if live, else release).
  `send()` routes to onboarding when init failed instead of crashing;
  `/api/runs` polls carry a sequence guard; the collapsed-sidebar scrim
  dismisses on click; dictation no longer clobbers text typed while the mic
  is open.
- **Web `/help` renders as a structured command grid**: the wide ASCII table
  became rubble in the narrow chat column, so the web surface now receives a
  compact `command — description` format and renders aligned, wrapping rows
  (22 commands) with a tips footer. The terminal `/help` keeps its aligned
  table.
- 16 new regression tests (suite 698 passed): `tests/test_security_review_r2.py`
  plus contract updates across the security/gateway/e2e suites.
- **CI fix:** `saturday doctor --provider ollama` exits 1 when the provider
  isn't running — which is always true on CI runners, so the CI doctor step
  was red by construction. New `--offline` flag skips the endpoint probe
  entirely (harness/config/workspace checks still run); CI now uses
  `doctor --provider ollama --offline`.
- Docs: `docs/launch-review.md` (engineering + wiring + VC + design review)
  and `docs/competitive-2026-08.md` (landscape brief with verified-status
  table).

### Competitive-parity UI round 8 (feature additions)
- **AI follow-up suggestions** (Devin/Cursor parity): after each completed
  turn, a tiny one-shot model call proposes 3 next-step prompts, rendered as
  compact chips above the composer; clicking one sends it. A new
  `POST /api/suggest` reads the session's last user/assistant exchange via
  `hydrate_session`, strips list markers, dedupes and caps the lines.
  Best-effort by design: model errors, unknown sessions and the feature
  being off all return an empty payload silently. Gate: new
  `suggest_followups` config (default on, Settings -> General checkbox,
  propagates through the derived cfg-sync list like its auto-title
  sibling). Chips clear on typing, send, session switch and new runs.
- **Per-session composer drafts** (Cursor/ChatGPT parity): whatever you type
  is kept per chat in localStorage and restored when you return — switching
  sessions no longer loses an unsent message. Cleared on send/queue.
- **Detached-run finish badges** (background-agent UX): leaving a running
  chat (round 4) now marks it; a 6s poll of `/api/runs` detects a detached
  run finishing and tags its sidebar row with a green "finished" badge until
  you open it — plus the completion ping / a desktop notification when the
  window is hidden (same settings as existing completion alerts).
- **Image lightbox** (Devin/ChatGPT parity): any transcript image (attached
  thumbnails, screenshots) opens in a full-screen overlay on click; Esc,
  outside click or clicking the image closes it, and the approval Y/A/N
  shortcut is suppressed while it is open.
- Round-8 tests in `tests/test_competitive_ui.py` (3; suite 690):
  `/api/suggest` parsing, empty-payload paths (off/unknown/model-failure),
  the config gate round-trip, and frontend wiring for all four features.

### Competitive-parity UI round 7 (composer close-up)
- **Tool buttons moved to the composer's bottom-left; send pinned
  bottom-right** (ChatGPT/Claude placement): enhance/mic/attach used to sit
  in a right-edge cluster in front of the send button, so the cluster (and
  the send button with it) jumped left the moment the prompt-enhancer wand
  appeared with typed text. Flex ordering now keeps conditional tools left
  of the hint — the send button never moves.
- **Uniform icon chrome in the action row**: the ghost wand/paperclip glyphs
  were visually inconsistent with the filled send square; composer icon
  buttons are now 28px with 15px glyphs (against the 30px send).
- **Disabled send is a dimmed accent button** instead of a dead grey square
  that read as a random dark blob in the corner; hover/active states are
  unchanged, and the busy (stop) state never overlaps the disabled style.
- **Breathing room**: the first text line had 1px of headroom under the
  mode chips (now 4px), and the placeholder's double space is fixed.
- Round-7 tests in `tests/test_competitive_ui.py` (1; suite 687).

### Competitive-parity UI round 6 (spacing & placement)
- **The invisible Preview pane no longer steals half the stage** (layout bug
  found by measuring the live DOM): `#stagePreview { display:flex }` outranked
  `.stage-pane { display:none }`, so the hidden preview pane permanently took
  50% of the stage width and squeezed every tab — Workbench, Activity,
  Changes, Plan — into the left half with a dead zone on the right. Only the
  active pane now lays out (`#stagePreview.on`); the Workbench dashboard's
  centered column finally centers across the full stage width.
- **One sidebar gutter**: the sidebar had four different left edges (New chat
  12px, filter/session rows 8px, projects header 16px, footer 14px). All
  regions now share the 12px gutter and session-row text aligns with the
  New chat button text; the footer version label no longer wraps to two
  lines mid-word.
- **Composer edges align**: the plan/safety chips sat 10px right of the
  message text they belong to; they now share the textarea's left edge
  (Cursor/Cline/ChatGPT alignment).
- **Stage tabs use the topbar's 12px gutter** instead of 10px, and the
  Workbench info values prefer natural break points (`overflow-wrap`) over
  mid-word `break-all` splits ("harn ess").
- **Toasts open below the header bar** (top 48px) instead of at the viewport
  top edge, where they covered the model/safety pills and menu triggers.
- Round-6 tests in `tests/test_competitive_ui.py` (2; suite 686): a
  regression guard on the stage-pane display rule and the spacing-system
  invariants.

### Competitive-parity UI round 5 (button / dialog / dropdown placement)
- **Dropdowns now anchor to their trigger** (Cursor/ChatGPT/Claude placement):
  every menu previously opened at a hard-coded viewport corner
  (`top:42px; right:10px`), so the composer's safety-mode chip produced a menu
  pinned to the far top-right, disconnected from the control that opened it.
  A shared `openDropdown()` helper positions each menu below its trigger,
  edge-aligned, flips it above when there is no room below, clamps it to the
  viewport, and falls back to the kebab anchor if the trigger is unmeasurable.
  Rewired: session (kebab), model, theme, safety (chip and header badge), and
  move-to-project menus.
- **Menus are mutually exclusive**: opening one closes the others (the kebab
  menu used to open on top of an open safety menu). Menus also close on
  window resize and transcript scroll instead of drifting away from their
  trigger, and the trigger toggles its own menu closed.
- **Native `confirm()`/`prompt()` replaced with an in-app dialog** (dialog
  parity; native dialogs are unstyled and unreliable inside the desktop
  shell): a small centered `askModal` with themed Cancel (new `.secondary-btn`
  style — the trust modal's "Don't Trust" button was previously a raw
  browser button) and a filled-red destructive button (`.danger-solid`,
  distinct from the outline `.danger-btn` used by settings footers).
  Enter confirms, Esc/Cancel/outside-click dismisses, the input is focused
  and prefilled for prompts. All ten former native call sites migrated:
  undo last edit, journal restores (list + compare), delete session, rename
  session, clear-all double confirm, archive, delete project.
- Round-5 tests in `tests/test_competitive_ui.py` (4; suite 684): anchored
  helper wiring for every menu, menu exclusivity, a regex guard that fails on
  any bare native `confirm(`/`prompt(` call, and the dialog/button styling.

### Competitive-parity UI round 4 (common-sense UX / placement)
- **Switch sessions while one is running** (Devin/Cursor parallel parity): the
  blocking "wait for the current run" guards on New chat and session switching
  are gone. Switching away detaches the reader (the run continues server-side
  and stays watchable in Runs); opening a busy session re-attaches to its live
  event tail — the in-flight turn is replayed from its first event
  (`/api/stream/<sid>?from=run`, backed by a new `run_start_seq` stamp) and
  then streams live, including approvals.
- **Plan & Safety moved to the composer** (Cursor/Cline placement): a mode
  cluster above the input hosts a Plan toggle (previously plan mode was
  invisible until already on, reachable only via /plan) and a Safety chip.
- **Safety mode is now an explicit menu** (misclick safety): the header pill
  used to cycle modes on click — one accidental click away from "yolo".
  Both the pill and the composer chip open a 4-option menu with plain-language
  descriptions of each mode.
- **Esc stops a running agent** (Claude Code/Amp parity): after overlays are
  dismissed, Esc requests a stop.
- **Contextual composer hint**: "approval waiting — Y/A/N" while an approval
  or question card is pending; "working — Enter queues a follow-up · Esc
  stops" mid-run; standard hint when idle (assistant mode keeps its own).
- **Settings gear in the sidebar footer**: Settings was previously reachable
  only through the session ⋯ menu.
- **Composer refocus** when a run finishes and nothing else holds attention.
- **Session filter**: Esc clears and blurs.
- **Narrow-window guards**: header badges shed progressively below 1140/980/
  860px instead of overflowing.
- Round-4 test: the re-attach stream contract (replay + live continuation +
  shared `done`) in `tests/test_competitive_ui.py` (21 there; suite 666).

### Competitive-parity UI round 3 (interactive core)
- **`ask_user` clarifying-question tool** (Lovable question cards / Windsurf
  parity): a new builtin tool lets the agent stop and ask the human a question
  with 2-8 one-click options plus free text, blocking until answered (fail-open
  timeout: the model proceeds with best judgment). The web surface renders an
  interactive card resolved via `POST /api/ask`; headless surfaces get a
  graceful fallback instead of a stall. Non-mutating, so it is available in
  plan mode.
- **Deny with feedback** (Claude Code parity): approval cards gain a "+ note"
  field; the note travels through the safety gate into the tool denial
  message (`user denied: … \n user note: …`) so the agent corrects course
  instead of retrying the identical call.
- **AI-generated session titles** (Zed/OpenHands/Goose parity): after a fresh
  session's first completed reply, a tiny background one-shot call renames the
  chat (never overwrites a user rename; disable via Settings → General or
  `auto_title_sessions: false`). The sidebar and title bar update live via a
  `title` event.
- **Live subagent progress** (Claude Code subagent rows / Warp pills parity):
  child `task` runs now stream step/tool/done events attributed to the parent
  tool card as nested rows; forwarded only when the child's `run()` supports
  the callbacks, so third-party/fake agents are unaffected.
- **Prompt enhancer** (Bolt parity): a composer wand button rewrites the draft
  message into a clearer, structured prompt via a one-shot call (`POST
  /api/enhance`); click again within 60 s to undo.
- **Per-session model override** (Cline/Amp parity): with a chat open, the
  model menu applies to that chat only (`POST /api/config` with
  `session_id`); the pill shows the effective per-chat model, and the global
  default is untouched. Powering it: `AppState.session_models` consulted by
  `_cfg_for_session`, with idle-agent rebuilds.
- New plumbing: `WebApprover.ask_question` + note-aware `resolve`,
  `safety._user_denial`, `SessionStore.set_task`, `AskUserTool`
  (`tools/ask.py`), `LLMClient`-based `_one_shot` helper. Round-3 tests in
  `tests/test_competitive_ui.py` (19 total there; suite now 664).

### Competitive-parity UI round 2 (from the same 22-product research)
- **Runs monitor** (Warp Agents Panel / Cursor Agents Window / Codex task-list
  parity): a new stage tab lists every session with live status (running /
  stopping / idle), model, project and uptime; busy sessions sort first and
  can be stopped from the panel. Backend: `GET /api/runs` plus per-runtime
  `run_started_at` stamps.
- **Queue management** (Cursor / Bolt / Goose parity): queued messages are now
  drag-reorderable and clickable to edit (removes from queue into composer).
- **Plan approval gate** (Replit "Accept tasks" / Jules parity): the Plan tab
  shows *Approve plan & switch to Act* while plan mode is on.
- **One-click "fix this"** (v0 "Fix with v0" / Lovable "Try to fix" parity):
  every failed tool card offers a repair action that hands the error back to
  the agent.
- **Review changes + attach** (Codex /review, Cursor Bugbot, Warp parity):
  the Changes tab gets a *review changes* button (asks the agent to review its
  own edits), and each file card gains *attach* (insert the diff as composer
  context) and *open* (view current contents).
- **Git status chip** (Warp git-diff-chip parity): read-only branch/changed/
  +/− summary of the workspace repo in the Changes toolbar (`GET
  /api/git/status`; runs `git status`/`diff` with fsmonitor disabled).
- **Journal compare** (Cline "Compare" / Replit preview parity): each journal
  entry gets a *compare* button rendering a before/after line diff in a modal
  with a restore action (`GET /api/journal?entry=N` returns the snapshot).
- **Session archive** (OpenHands / Zed parity): sessions can be archived from
  the session menu — hidden from the sidebar, restorable via a *show archived*
  toggle (`POST /api/archive`).
- **Model favorites** (Zed parity): star models in the model menu; Alt+M
  cycles favorites, Ctrl+M opens the menu.
- **Live app preview** (Replit / OpenHands App-tab parity): the Preview tab
  gains a URL bar that embeds a running dev server in a sandboxed iframe,
  remembered per session.
- **Approval sound** (Cline parity): the completion sound now also plays when
  an approval is waiting in a hidden window.
- Minor: shortcuts overlay documents Ctrl+M / Alt+M; archived sessions show a
  tag in the sidebar.

### Competitive-parity UI round 1 (researched across 22 agent products)
A UI/UX audit of Claude Code, Codex, Cursor, Windsurf, Cline, Copilot, Gemini/Jules,
Amazon Q/Kiro, Aider, Continue, Devin, OpenHands, Manus, Goose, Warp, Amp, Zed,
Replit Agent, Bolt.new, v0, Lovable and Trae/Roo produced the recurring surfaces
Saturday lacked. All are now implemented:

- **@-file mentions in the composer** (Cursor/Claude Code/Cline/Warp parity):
  typing `@` opens an autocomplete of workspace files (recursive, cached 30 s);
  picking inserts the workspace-relative path.
- **Branch-based history surgery** (Goose/OpenHands/Amp parity): every user
  message gains *Edit & resend* (fork without that exchange, resend there) and
  *Branch from here*; the assistant stats line retry now forks instead of
  re-asking in place. Backend: hydration items carry `msg_idx` (raw message
  index) so `/api/branch keep=` truncates exactly, tool messages included.
- **Find in chat (Ctrl+F)** (Goose parity): highlight-all search over the
  transcript with match count and Enter/Shift+Enter navigation.
- **Per-edit restore from the file journal** (Cline/Roo/Zed/Cursor parity):
  the Changes tab gains *undo last edit* and a *restore history* panel listing
  journaled edits (newest first) with one-click restore via the existing
  journal machinery — including new-file tombstones and the truncated-snapshot
  / privileged-path refusals.
- **In-session cost display** (Cline/Goose/Claude Code parity): each done turn
  carries a list-price estimate + running session total (from the existing
  MODEL_PRICING table; unknown models stay silent). Shown on the stats line
  and the token-meter tooltip.
- **Custom slash commands / prompt library** (Warp Drive/Continue/Kiro
  parity): `~/.saturday/commands.json` maps `/name` to a prompt template
  (`$ARGS` inserts the text typed after the command); managed in Settings →
  Commands and merged into the `/` autocomplete.
- **Scheduled automations UI** (Manus/Goose/Warp parity): the CLI's cron store
  is now surfaced in Settings → Schedules (add/remove/list) and fired by an
  in-app watcher thread (opt-out `SATURDAY_SCHEDULE_WATCHER=0`); due schedules
  run as one-shot agent runs with the CLI's log format.
- **Compact now** (Goose parity): a button in the context panel runs /compact
  and refreshes the breakdown.
- **Approval attention flash** (Cline/Devin parity): when an approval arrives
  while the window is hidden, the title bar pulses until you return.
- **Export as HTML** (OpenHands parity): self-contained, styled transcript in
  the session menu alongside Markdown/JSON.
- **Per-turn 👍/👎 ratings** (Lovable parity): recorded locally in
  `feedback.jsonl` as a reward signal for the training-data flywheel.
- **Drag & drop text files** attach like images (dropped text becomes a code
  block in the composer).

New endpoints: `GET /api/journal`, `POST /api/journal/restore`,
`GET/POST /api/schedules`, `POST /api/commands`, `POST /api/feedback`;
`/api/state` now includes `pricing`, `custom_commands` and
`schedules_watcher`. Covered by `tests/test_competitive_ui.py` (8 tests) on
top of the full 653-test suite.

### Design (system design review)
- **Config propagation is now derived, not hand-maintained**: the web UI's
  per-session cfg sync list is computed from the settings schema (everything
  propagates except genuinely project-owned fields), and settings that are
  captured into tool instances at construction (`verify_command`,
  `lsp_servers`, `memory_max_chars` — plus the existing `auth_scopes`) now
  trigger an agent rebuild. Previously a `verify_command` change in Settings
  reached `agent.cfg` but never the file tools, so it silently never ran for
  live sessions; a drift-guard test pins the invariant.
- **`sandboxed` no longer waives approvals without enforcement**: no
  container/job-object isolation executor ships in this build, so flipping
  the flag used to silently skip guardrail + dangerous-pattern asks with
  nothing in return. The effective value is now `cfg flag AND
  isolation_enforced()` (a documented extension point for future executors),
  and an unmet request surfaces a one-time warning.
- **`shell_allow_network` is wired in** (it was configurable and exposed in
  Settings but consumed by nothing): the shell tool reads it dynamically per
  call, enforces no-network via `unshare --net` on POSIX, and fails closed
  with an explanatory refusal on platforms where isolation cannot be
  enforced (Windows). Background jobs honor the same wrapping.
- **Hook composition is chain-only**: `install_web_surface` chains a
  pre-existing `pre_tool_call` hook instead of replacing it (verified: a
  pre-existing hook was silently dropped).
- **Resource bounds**: EventBus subscriber queues are bounded with
  drop-oldest overflow (a stalled stream client no longer grows the process
  without limit), the desktop app evicts idle runtimes beyond a cap (the
  runtime map was unbounded for the life of the process), and language-server
  clients are closed at exit (parity with the MCP atexit net).
- **Layering**: file-edit domain logic (`FILE_EDIT_TOOLS`, diff renderer,
  `_norm`) moved to `saturday.editing` so the web surface no longer imports
  the terminal surface; `/help` text moved into the shared slash registry
  (both with compatibility re-exports). An AST-based test forbids
  surface-to-surface imports.

### Fixed (functionality review)
- **`memory_search` was dead on real sessions** (found by indexing a real
  transcript): the recall index only understood a flat `{"role", "text"}`
  record shape that no writer produces — the agent loop appends
  `{"type": "messages", "messages": [...]}` — so every real session indexed
  zero rows and searches always returned nothing. `recall.rebuild` now
  unwraps the SessionStore shape (flat records still indexed for
  compatibility), so cross-session recall actually works.
- **`grep`/`glob` could escape the workspace** with a `..` pattern: glob
  joins `..` lexically, so `include="../x"` listed and READ files outside the
  workspace root (read_file/list_dir refuse; grep returned their contents).
  Both tools now resolve every match and skip anything outside the root.
- **Cron schedules with day-of-week 7 (Sunday) never fired**: the validator
  accepts the standard-cron 0-and-7-both-Sunday convention, but the matcher
  only ever tested Sunday as 0. `0 9 * * 7` now fires on Sundays.

### Security
- **Privileged-path guard extended to all Saturday state files** (multi-round
  security review, exploit confirmed with PoC): `write_file`/`edit_file` now
  refuse `.saturday/hooks.json` (shell commands executed on every tool call),
  `config.json` (safety_mode / verify_command), `approvals.json` (the agent's
  own authorization store), `schedules.json`, `trusted_projects.json`,
  `projects.json`, `usage.jsonl` and `SOUL.md`, in addition to the already
  blocked `.env`, `.saturday/mcp.json` and `file_journal.jsonl`. Previously an
  agent could convert an (often unasked) workspace file write into persistent
  arbitrary command execution by planting a project-level `hooks.json`.
- **Journal restores refuse privileged targets**: `/revert` and `/rewind`
  (`journal.restore_entry` / `restore_to_length`) now refuse journal entries
  whose target is one of the state files above — entries are model-influenced
  data, and a poisoned entry could previously plant hook content via a restore
  (PoC-confirmed path).
- **`saturday app --no-token` now prints the same explicit warning** as
  `saturday serve --no-token` (the endpoint drives a full-capability agent).

### Added
- **Stage tabs upgraded** (desktop web UI right panel):
  - **Workbench** is now a live run dashboard: step / tool calls / tokens /
    files-changed / elapsed counters update in real time, with a "latest
    activity" list (last 6 tool calls; click one to jump to its transcript
    card). Info grid gains the palette shortcut; the model cell now refreshes
    on `/api/config` changes.
  - **Activity**: client-side filter box, clear button, "N calls · M running"
    summary and a per-entry copy button (tool + arguments + output) for bug
    reports.
  - **Changes**: newest file first (auto-switch shows the latest edit without
    scrolling), per-file collapse toggles, a "+A −D across N files" summary,
    per-file copy, unique-path badge counting (re-editing a file no longer
    inflates the badge) and an 800-line render cap for huge new files.
  - **Preview**: persistent thumbnail strip with active highlight, screenshot
    counter, open and download buttons, capped at 30 stored screenshots.
  - **Plan**: the todo output renders as a structured checklist (goal,
    progress bar, done/pending steps, updated time) with the raw output kept
    in a collapsible section; unparsable output still falls back to plain text.
  - **Files**: clickable breadcrumb path, refresh button (the tab re-lists on
    every open so agent-written files appear), per-folder filter box, modified
    time column, image preview for png/jpg/gif/webp/bmp and item count.
- `GET /api/ws` entries now include `mtime` and absolute `path` fields
  (consumed by the Files tab; purely additive).

### Fixed
- `/api/file` crashed the connection with `TypeError` whenever a `sid` was
  supplied and the session had no project workspace (`session_workspace()`
  returns `None`; `Path(None)` raised) — image previews were broken for all
  projectless sessions.

## 0.8.0 — provenance, verify hooks, approval memory, metrics, polish

### Added
- **Provenance marking** (`saturday/provenance.py`): machine-readable
  `provenance` blocks on exported trajectories, eval artifacts and audit
  bundles (GB 45438-2025 / EU AI Act Art. 50 aligned): `ai_generated`,
  generator + version, provider/model, session id, timestamp and a SHA-256
  content fingerprint that detects post-hoc edits. Modes via
  `provenance_marking` config / `SATURDAY_PROVENANCE`: `metadata` (default),
  `visible` (adds a disclosure footer to answers, webui done events included)
  and `off`. Settings > Data pane exposes the selector.
- **Post-edit verify hook**: `verify_command` config / `SATURDAY_VERIFY_CMD`
  runs after every successful write_file/edit_file (`{path}` substituted) and
  feeds output back inline so the agent self-corrects next step; skipped when
  the free ast syntax check already failed.
- **Per-(action,target) approval memory for desktop tools**: saved allow rules
  now match pointer/keyboard/clipboard/window/app_open signatures (exact or
  `prefix*`) in `check_command`, so "always" stops repeat asks without
  widening to everything; deny mode and background-only structural gates
  still outrank rules.
- **Usage metrics v2**: success rate, avg tokens/turn, outcome breakdown and
  per-provider turn counts in `usage_summary`; new `GET /api/metrics`
  endpoint; About pane renders completion health; `/metrics` slash command in
  repl AND webui; CLI runs and REPL turns now record usage too (previously
  only the webui did — metrics silently undercounted).
- **`saturday init`**: scaffolds AGENTS.md template plus
  `.saturday/mcp.json.example` and `hooks.json.example`; idempotent,
  `--force` to overwrite.
- **Export compression**: `saturday export --compress TOKENS` replaces older
  oversized tool results with short omission markers (goal, bookends and the
  recent half stay verbatim; a `compression` meta block records before/after).
- Doctor now validates local JSON files (config/hooks/approvals) instead of
  letting silent fallbacks hide corruption; unknown providers get a
  "did you mean?" suggestion.

### Fixed
- `job_list` crashed with `AttributeError` once any background subagent job
  existed (`JobManager.reap` assumed `.proc` on duck-typed AgentJobs).
- Servers ignoring `stream:true` returned plain JSON through the streaming
  path without Hermes XML extraction — non-native tool-callers silently lost
  tool calls in streamed mode (now parsed exactly like `_chat_once`, with a
  `tool_call` stream event per call).
- DeepSeek `<｜Assistant｜>` payloads with a misplaced `</think>` closer are
  left untouched instead of mis-slicing content/reasoning.
- REPL `/model` now persists to config.json (was in-memory only).
- Plan mode no longer hides observation tools `repo_search`,
  `lsp_diagnostics`, `lsp_definition`.
- Compaction token estimates count assistant tool-call argument bytes;
  large parallel-call turns no longer slip past the compaction threshold.

### Performance
- `ToolRegistry.specs()` cached per registry (invalidated on mutation):
  ~84x faster on the every-step path with the default plugin set.
- Session meta first-line cache (stat-stamp validated): sidebar refreshes no
  longer re-open every session file (~1s at 150 sessions cold -> ms warm);
  seeded on create, invalidated by rename/set_project flows via stamp guard.
- AGENTS.md/CLAUDE.md rules block cached per mtime (was read on every run).
- Settings saves no longer rebuild a full agent just to enumerate tool names
  (cached validation universe).
- LLM request body encoded once per candidate model instead of once per retry.

## 0.6.0 — context transparency, omarchy themes, assistant mode, guardrails

### Added
- **Context breakdown** (`saturday/context.py`): per-section token accounting
  (system prompt tiers, tool schemas, user/assistant/tool messages, images,
  reply headroom) against the compaction threshold and model budget.
  `GET /api/context`, `/context` slash command (webui + repl), clickable token
  meter with a stacked-bar panel, and live `ctx` events every step.
- **19 Omarchy Linux themes**: generated from the official basecamp/omarchy
  palettes (Tokyo Night, Catppuccin, Lumon, Ethereal, Everforest, Gruvbox,
  Miasma, Hackerman, Osaka Jade, Kanagawa, Nord, Matte Black, Vantablack,
  Ristretto, Retro 82, Flexoki Light, Rose Pine, Catppuccin Latte, White);
  theme menu on the toolbar, settings select with optgroup, "system" follows
  the OS; toggle flips between the last dark and last light theme.
- **Personal assistant mode (v2)**: full capability with a plain-language
  surface. The registry is identical in both modes - the assistant can use
  the whole machine. Differences are UX + defaults: background-first computer
  use enabled with the mode (non-intrusive ui_invoke / window-targeted input /
  minimized launches), tool cards show friendly actions ("searching the web",
  "opening an app") with details one click away, no auto-jumping stage tabs,
  outcome-only turn stats, a topbar assistant badge, task-flavored suggestions,
  and a persona that does the whole job end-to-end and reports results instead
  of narrating commands. Toggle in Settings > General or `--assistant`.
- **Assistant identity (JARVIS-style)**: name your assistant and set how it
  addresses you; it injects a calm, mission-debrief voice with light wit.
  Hands-free voice loop: auto-send when you stop talking + spoken replies
  default on in assistant mode. Live "working… 42s" heartbeat replaces step
  counters; one-click task chips execute immediately.
- **Destructive-action guardrails**: DROP DATABASE/SCHEMA/TABLE/COLLECTION,
  TRUNCATE, FLUSHALL, git reset --hard / clean -fdx, recursive deletes,
  SQL DELETE/UPDATE without WHERE ask even when safety is off and fail closed
  without an approver (`destructive_guardrails` config / SATURDAY_GUARDRAILS).
- **Database auto-backup**: destructive shell commands referencing *.db /
  *.sqlite* files snapshot them to `<workspace>/.saturday/backup/` first.
- **Write verification**: python files written/edited are ast-syntax-checked;
  warnings appended inline so the agent self-corrects.
- **Cross-chat search**: Ctrl+K palette searches message content across all
  saved sessions (`GET /api/search`).
- **Onboarding wizard**: first-run provider + API key setup stored in
  `~/.saturday/.env`.
- **Per-project memory**: project chats read/write `.saturday/MEMORY.md`
  inside the project workspace, layered over global memory.
- **Local usage metrics**: tokens by day + per-model totals from
  `usage.jsonl`, rendered in Settings > About. Nothing leaves the machine.
- `doctor --privacy` data-flow report; `run --ci` CI mode (deny approvals,
  quiet, `CI RESULT: PASS|FAIL`, exit code).

### Fixed
- Approval ids were per-runtime counters resolved against ALL runtimes:
  two concurrent sessions could approve each other's commands (one allowed
  wrongly, the other failed closed). Ids are now session-namespaced.
- Editing a project (scopes/workspace/instructions/knowledge) never resynced
  live runtimes - tightened reserved scopes were silent no-ops until restart.
- `/api/context` minted permanent cached runtimes for arbitrary sids.
- Chat streams could hang ~forever: done was published before the busy flag
  cleared, racing the stream pump's exit condition.
- Failed tool results carried prose after the JSON body, so reopened sessions
  rendered errors as successful green results.
- Bus replay was positional over a wrapping ring buffer; long sessions could
  drop the just-sent message from replay. Events now carry monotonic seqs.
- Keep-alive socket aborts (WinError 10053) spammed stderr whenever the app
  window closed mid-request.

## 0.5.0 — platform surfaces: gateway, serve, screen, JS browser, TUI

### Fixed (manual code review pass)
- Compaction could split an assistant tool-call from its tool result at the
  tail boundary, producing API-invalid orphaned tool messages; the cut now
  walks back to a safe pair boundary.
- Streaming retry/fallback re-emitted already-streamed deltas after a
  mid-stream failure; retries are now suppressed once any delta was delivered
  to avoid duplicate output.
- Safety hardline missed `rm -rf / --no-preserve-root`; bypass flag now
  blocked explicitly and root pattern no longer anchored to end-of-command.
- Agent.run() stacked new callback wrappers onto shared hooks every call;
  long chat sessions compounded N lambdas per callback after N turns. Hooks
  are now composed fresh per run.
- MCP client deadlock: a timed-out tools/call left the worker blocked in
  readline holding the protocol lock, wedging all later calls. Timeouts now
  kill the server process; the next start() respawns cleanly.
- PythonREPL accepted a timeout parameter but never enforced it; infinite
  loops hung the agent permanently. A watchdog now restarts the interpreter.
- Telegram gateway polled silently every 1.5s even when auth failed; failures
  now back off exponentially (cap 30s) with stderr warnings.
- default_registry wired shell background jobs to one JobManager and the
  job_* tools to another, making started jobs invisible to job_list.

### Added
- **Telegram gateway** (`saturday gateway`): zero-dependency long-polling bot;
  per-chat agent sessions (1h idle recycle), chat-id allowlist, chunked
  replies. Transport injectable for offline tests.
- **HTTP serve mode** (`saturday serve`): POST /message {"text"} -> JSON
  answer; wire any chat/orchestration front-end to a Saturday agent.
- **Computer-use lite**: `screen` tool captures the real display and attaches
  the PNG for vision inspection. Windows via built-in PowerShell/.NET capture
  (no deps); elsewhere via optional `pillow` (`saturday[desktop]`).
  Verified live on Windows.
- **JS-rendered browser** (`web_browser_js`): Playwright adapter with open /
  html / click-by-text / full-page screenshot actions; screenshots attach as
  vision images. Optional `saturday[browser]` extra — registered only when
  playwright is importable so models never see unusable tools.
- `saturday tui`: alternate-screen console with header/status bar, colored
  streaming, step/token telemetry after every turn.

## 0.4.0 — web frontier, vision, skills loop

### Added
- **Web tools**: `web_search` (DuckDuckGo Lite scraping with uddg-link
  unwrapping) and text-mode `browser` (open/click/back over readable-text
  extraction; honest limitation: no JS rendering — use an MCP Playwright
  server for that). `web_fetch` now returns extracted readable text.
- **Multimodal input**: `Agent.run(attachments=[...])` / `saturday run
  --image` build OpenAI vision content parts (base64 data URLs); `view_image`
  tool attaches local images to the next observation via ToolResult.images.
- **Skills loop** (hermes-style learning): `~/.saturday/skills/<id>/SKILL.md`
  store with `skill_save`/`skill_load`/`skills_index` tools; index injected
  into the volatile prompt tier; agent nudged to load before inventing and to
  capture after mastering procedures.
- Console UI: ANSI colors (reasoning dimmed, tool results green/red), spinner
  in quiet runs, chat `/attach` + `/images` commands.

## 0.3.0 — MCP + durable execution

### Added
- **MCP client support** (stdio, JSON-RPC 2.0): initialize handshake,
  tools/list, tools/call with per-call timeout guard; remote servers bridge
  into the native tool registry via `McpToolProxy` (collision-safe aliasing).
  Configure via `.saturday/mcp.json` or `AgentConfig.mcp_servers`; inspect
  with `saturday mcp`. Windows fix: bare `.py` server commands auto-prepend
  the current interpreter.
- **Durable run checkpoints**: history snapshot after every agent step
  (`on_checkpoint` hook), atomically persisted per session
  (`<id>.checkpoint.json`). Interrupted runs resume mid-trajectory via
  `chat --resume` or `Agent.run(initial_history=...)` — LangGraph-style
  durability in ~60 lines.
- `run --session ID` for named resumable sessions.

## 0.2.0 — launch-candidate hardening

Audited against cloned sources of deepseek-ai/deepseek-harness and
NousResearch/hermes-agent (see GAP-REPORT.md); closed the highest-leverage gaps.

### Added
- Safety layer (`saturday.safety`): hardline blocklist (rm -rf /, mkfs, dd to
  raw device, fork bomb, shutdown...), recoverable-dangerous class with
  ask/deny/off modes, approver callbacks that fail closed; wired as default
  `pre_tool_call` gate on shell.
- Error classification in the LLM client: auth / rate_limit (Retry-After
  honored) / context_overflow / server / network; jittered backoff; fallback
  model chain (`AgentConfig.fallback_models`); context overflow triggers forced
  compaction then a single retry.
- Session persistence (`saturday sessions`, `chat --resume`) as JSONL under
  the Saturday home dir, mirroring dsh's default session-persistence-jsonl.
- Compaction upgrade: original goal preserved verbatim through compaction,
  structured progress template, optional aux-LLM summarizer hook.
- Shell output spill files: outputs >16KB keep an 8KB tail and spill the full
  text to `.saturday/spill/`.
- Reasoning passback flag (`keep_reasoning_in_history`) emitting dsh-style
  `reasoning_content` in history.
- Persistent memory tool (`memory` + MEMORY.md) loaded into the volatile prompt
  tier.
- Three-tier system prompt assembly (stable/context/volatile) for provider
  prompt caching, hermes-style.
- `saturday doctor`: python/config/key/endpoint/workspace/registry preflight.
- Wire-level test suite: real HTTP against a local OpenAI-compatible mock —
  SSE parsing, fragmented tool-call delta reassembly, auth headers, usage events.
- Dataset export filters trajectories using unregistered tools
  (`--keep-unknown` to disable), hermes batch_runner parity.

### Fixed
- Streaming client silently returning empty content when a server responds
  with JSON despite `stream:true`; now sniffs content-type and handles both.
- Byte-at-a-time stream reads replaced with single buffered read.
- Python REPL framing race where a raising payload desynced the protocol;
  sentinel now emitted by the interpreter process itself.
- Path sandbox now resolves symlinks before the workspace-root check.
