# Roadmap toward v0.9.1

Living list, updated as items land or scope changes. Sectioned by where an
item came from so it stays traceable back to the actual decision, not just
a flat backlog. This is a lot of ground for one point release — expect it
to get trimmed/resequenced, not built end to end in one pass.

## Carried over, still pending

Nothing below has been started. Ordered roughly by dependency, not
importance — the update system gates real self-update testing, and the
skills work is explicitly required before SWE-bench per direct instruction.

1. **Omarchy-inspired update/release system.** Version-check against the
   latest GitHub release, delegated self-update per install channel (pip
   wheel vs deb vs rpm vs AppImage vs PKGBUILD vs the Windows installer all
   update differently), plus a timestamp-named migration-script system with
   marker-file state tracking for breaking changes between versions.
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
5. **Isolated app-window profile for the browser fallback.** `pywebview`
   needs system GTK (`gi`) or Qt (`qtpy`) for a true native window on
   Linux, which a PyInstaller bundle can't carry (isolated interpreter, no
   access to system site-packages) — expected, not the bug. The actual bug:
   `launch_app_window` in `webui.py` reuses the user's already-running
   Chromium session/profile instead of a genuinely separate chromeless
   window, because it doesn't set an isolated `--user-data-dir`. Cheap,
   scoped fix — was mid-fix when scope moved to this list.
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
- [ ] **Resource limiting.** No CPU/memory/wall-clock caps for a run left
      unattended. An autonomous agent that can run away with resources on
      a shared or cloud box is a real operational risk, not a hypothetical
      one.
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

- [ ] **Real self-update system.** This is item 1 above (Omarchy-inspired
      update/release work) — restated here because "can update itself
      without a human re-running an installer" is a load-bearing autonomy
      requirement, not a nice-to-have.
- [ ] **Update locking + a receipt log.** Concurrent updates (two triggers
      firing at once) need to be mutually exclusive, and every update
      needs a record of what changed and when — both for debugging and for
      the eventual rollback path.
- [ ] **Graceful self-relaunch after an update**, so a long-running
      instance doesn't need a human to notice a new version landed and
      manually restart it.
- [ ] **Model fallback.** If the configured provider/model errors or rate
      limits, an autonomous run currently just fails the task. Needs an
      explicit fallback chain, not a silent retry-forever loop.
- [ ] **Cost and data-policy guardrails per model**, checked before a call
      goes out, not just observed after the fact via the existing cost
      metrics.

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

- [ ] **Structured task decomposition**, distinct from a single-shot
      `run`. Breaking a goal into ordered sub-tasks with dependencies, not
      just handing the whole thing to one long agent loop.
- [ ] **A first-class subagent orchestration story.** Engine support
      already exists (`enable_subagents`); this is documenting and exposing
      it as a real, driven-by-the-planner surface, matching the pending
      item in the carried-over list above.
- [ ] **Cross-session goal tracking.** State that persists across
      restarts about what the agent is working toward, not just what
      happened in one session's transcript.

### Memory and recall

- [ ] **Session search across history**, not just the current one — find
      "what did I ask it to do last week about X" without manually
      scrolling sessions.
- [ ] **Active memory curation.** Right now memory is whatever's in a
      session file; nothing periodically consolidates or nudges the agent
      to persist durable facts versus letting them age out with the
      transcript.

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
- [ ] **A pluggable external-agent runner** — a generic interface for
      spawning another CLI agent (Claude Code, Codex, Cursor, Gemini CLI,
      others) as a delegate, installing it if it's missing, rather than one
      hardcoded integration per tool.
- [ ] **A visual stack-builder UI** for assembling a full agent
      configuration — models, tools, skills, MCP connections — without
      hand-editing config files. The most novel, least-scoped item here;
      needs its own design pass before implementation starts.

None of this is sequenced yet. The reliability and self-management
sections are the actual prerequisite for "leave it running unattended,"
which is the literal definition of autonomous — those two probably come
first regardless of what else gets reordered.
