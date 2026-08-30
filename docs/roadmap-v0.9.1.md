# Roadmap toward v0.9.1

Living list, updated as items land or scope changes. Sectioned by where an
item came from so it stays traceable back to the actual decision, not just
a flat backlog. This is a lot of ground for one point release — expect it
to get trimmed/resequenced, not built end to end in one pass.

`[ ]` not started · `[~]` partially landed, real gap remains · `[x]` done.

## Carried over, still pending

Nothing below has been started. Ordered roughly by dependency, not
importance — the update system gates real self-update testing, and the
skills work is explicitly required before SWE-bench per direct instruction.

1. [x] **Omarchy-inspired update/release system.** Landed as `saturday
   update` (see the self-management section below) — version check,
   channel detection, locking, receipts, self-relaunch.
2. **Skills / capability work, required before running SWE-bench-verified.**
   Direct instruction: don't run the benchmark until the harness is
   "capable enough with skills and other stuff." Covered in detail by the
   autonomy checklist below — this item is the gate, that section is the
   plan.
3. **`gbrain` / `gstack` research** (github.com/garrytan/gbrain,
   github.com/garrytan/gstack) — not yet actually looked at.
4. **PyPI Trusted Publisher setup.** Blocked on a human action on pypi.org
   — either add `MershLab/saturday` + `release.yml` as a trusted publisher,
   or generate a classic token for `gh secret set PYPI_TOKEN`. Exact steps
   in `docs/release-pypi-todo.md`. Every other v0.9.0 artifact (6 platform
   installers + GitHub Release) already shipped; this is the one remaining
   gap.
5. [x] **Isolated app-window profile for the browser fallback.** Landed:
   `launch_app_window` now sets its own `--user-data-dir`, so the app
   window is a genuinely separate Chromium instance instead of getting
   redirected into whatever regular browser session is already running
   (reproduced live, then fixed, 2026-08-30). `pywebview` still needs
   system GTK/Qt for a true native window on Linux — that's an inherent
   platform constraint, not this bug.
6. **Real Linux/Wayland computer-use gap.** The `xdotool`-based backend in
   `spatial_unix.py` is X11-only and confirmed not to work on a real
   Hyprland/Wayland setup. Flagged, not fixed.
7. **npm shim for discoverability — open question, not decided.** The
   actual harness is Python (pip/pipx); this would only ever be a thin
   wrapper shelling out to `pip install`, for npm-ecosystem visibility, not
   a real second implementation. Needs an explicit yes/no before building.
8. **Lower-priority items from the original competitive roadmap**
   (`docs/competitive-2026-08.md`), not urgent but not done:
   - Cost-per-task benchmark page
   - Subagent orchestration surface — documented, first-class story (engine
     support already exists via `enable_subagents`)
   - Plugin/skill hub — sharing format + directory
   - SEO/positioning — claim "harness-first" explicitly in README H1 and
     packaging copy

## Autonomy checklist

What "autonomous" actually requires, broken into build steps. Framed as
capability gaps, not features for their own sake — each item exists
because unattended, long-running operation breaks without it. Checked
against what Saturday has today; nothing here is double-counted against
the sections above.

### Reliability under no supervision

All 5 items landed 2026-08-30.

- [x] **Crash and session recovery.** Landed: `RunState` marker per session
      (status/pid/heartbeat), wired into `cmd_run`, the schedule watch loop,
      and the gateway dispatcher. A present marker with status=running and
      a dead pid is the crash signal, detected via `RunState.scan()`. Resume
      itself already existed (`chat --resume`); this is the detection layer
      that tells you which sessions need it.
- [x] **Heartbeat / liveness signal.** Same `RunState` marker's heartbeat
      field, refreshed on every tool-call via `on_tool_result`. Surfaced in
      `doctor` (orphaned-run listing + active-run count). Web UI Runs-tab
      surfacing still open — doctor covers the CLI/automation case for now.
- [x] **Resource limiting.** Landed: `--max-wall-seconds` (cross-platform,
      checked at each step boundary, same pattern as the existing
      `--max-run-tokens`) and `--max-memory-mb` (real OS-enforced RLIMIT_AS
      on a `--detach` spawn, POSIX only - Windows has no stdlib equivalent
      without a real dependency, ignored there with a printed note rather
      than silently doing nothing).
- [x] **Pause/resume as a distinct control**, separate from stop. Landed:
      `saturday sessions --pause/--unpause <id>`, file-based (not signals,
      so it's identical across platforms). Wired into `cmd_run` and the
      gateway's per-chat dispatch; deliberately not the scheduler, since it
      fires due schedules synchronously and blocking there would stall
      every other pending cron job.
- [x] **Startup self-audit.** Landed: `_preflight_check` runs before a
      `--detach` spawn (provider config + key presence, no network probe -
      that's a real wait on every detach for what's normally an
      already-working setup). Aborts before `subprocess.Popen` instead of
      spawning a process that fails minutes later in a log nobody's
      watching.

### Self-management

All 5 items landed 2026-08-30.

- [x] **Real self-update system.** Landed: `saturday update` — version
      check via GitHub's releases API, live-process channel detection
      (pip/pipx/deb/rpm/pacman/AppImage/Windows installer/macOS dmg),
      auto-applies only where safe without privilege escalation (pip/pipx),
      exact manual command everywhere else.
- [x] **Update locking + a receipt log.** Landed: mutual exclusion between
      a scheduled check and a manual run (dead-pid lock reclaim, same
      pattern as `RunState`), JSONL receipt per attempt.
- [x] **Graceful self-relaunch after an update.** Landed: a successful
      pip/pipx update offers `os.execv` self-relaunch in place.
- [x] **Model fallback.** Turned out to already exist in full —
      `LLMClient.chat` had a real, tested per-candidate retry/fallback
      chain with proper error classification. The actual gap was that
      `fallback_models` had no CLI flag; added `--fallback-models`. The
      original checklist entry was wrong about this until this pass
      checked the code instead of assuming.
- [x] **Cost and data-policy guardrails per model.** Landed:
      `--max-run-cost-usd` (dollar-denominated sibling of the existing
      `--max-run-tokens`, same step-boundary check, real list pricing,
      never fires on an unpriced model) and `--blocked-providers`/
      `--blocked-models` (checked before any client is built, filtered out
      of the fallback chain too — user-populated, not a built-in table of
      claims about what any given provider does with data).

### Reach — where the agent can be told to work and report back

- [ ] **Additional gateway platforms beyond Telegram** — Discord, Slack, at
      minimum. Multi-platform reach is what makes "runs while you're away
      and tells you what happened" actually useful day to day.
- [ ] **Webhook / external-trigger surface**, so other systems (CI, a cron
      job outside Saturday itself, another service) can kick off a run
      without going through chat or the scheduler.
- [ ] **Remote execution backends** — at minimum Docker and SSH, so an
      unattended run doesn't have to live on the same machine as the
      person who started it.

### Planning and delegation

Turned out this whole section already existed, checked against the actual
code on 2026-08-30 rather than assumed. Same root cause as the model-
fallback correction: this checklist was built by comparing feature lists,
not by reading Saturday's own source first.

- [x] **Structured task decomposition.** `TodoTool` (write/mark/read an
      ordered step list, checkpoint-persisted) — part of the default-
      enabled `workflow` plugin.
- [x] **Subagent orchestration.** `SubagentTask`/the `task` tool:
      continuable children with their own history, background execution,
      live progress forwarded to the web UI transcript, `enable_subagents`
      defaults to `True`. Already documented in `README.md` (tools list +
      a dedicated "Live subagent progress" feature callout) — no
      documentation gap either.
- [x] **Cross-session goal tracking.** `GoalStore` + `create_goal`/
      `get_goal`/`update_goal` tools, checkpoint-persisted via the same
      generic `export_state`/`import_state` mechanism `TodoTool` uses —
      also part of the default `workflow` plugin.

### Memory and recall

- [x] **Session search across history.** Turned out `search_sessions`
      already existed and was fully wired for the web UI (a real search
      box calling `/api/search`) — checked the code before assuming this
      was missing, same discipline as the last two sections. The one real
      gap was no CLI equivalent; landed `sessions --search`.
- [~] **Active memory curation, partially landed.** `memory_nudge_interval`
      re-surfaces the persistence reminder every N steps instead of once
      in the system prompt — real progress on the "nudge" half. Still
      open: actual consolidation/summarization when the memory file grows
      large (today it silently truncates on read at 8000 chars; the file
      itself can still grow unbounded on disk). That needs a real LLM
      summarization call with its own cost/design tradeoffs, deliberately
      not rushed into this pass.

### Extensibility

- [ ] **MCP server mode** — expose Saturday itself as something other
      tools (any MCP-speaking client) can connect to, not just a client of
      other MCP servers. Restated from the existing competitive-roadmap
      gap; this checklist is why it matters for autonomy specifically —
      other systems delegating work in.
- [ ] **A curated MCP catalog/picker with basic security screening**
      before connecting to a new server, instead of trusting whatever's
      configured.
- [ ] **A skills hub** — sharing format + directory, restated from the
      carried-over list; skills are how the agent gets *better* at
      recurring autonomous work over time instead of re-solving it from
      scratch each run.
- [x] **A pluggable external-agent runner.** Landed: `external_agent` tool
      + `ExternalAgentSpec` registry (Claude Code, Codex, Cursor, Gemini
      CLI), auto-installs on `install=true`. Registry-based so adding
      another agent or fixing a wrong invocation flag later is a one-line
      change. Claude Code's own flags are exact; the other three are
      best-effort against currently-documented behavior — called out
      honestly, not presented with false uniform confidence.
- [ ] **A visual stack-builder UI** for assembling a full agent
      configuration — models, tools, skills, MCP connections — without
      hand-editing config files. The most novel, least-scoped item here;
      needs its own design pass before implementation starts.

None of this is sequenced yet. The reliability and self-management
sections are the actual prerequisite for "leave it running unattended,"
which is the literal definition of autonomous — those two probably come
first regardless of what else gets reordered.
