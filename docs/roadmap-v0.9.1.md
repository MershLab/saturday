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
   "capable enough with skills and other stuff." Ties directly into the
   `optional-skills/` library found in `MershLab/harness` — likely the
   fastest real path to satisfying this, not a from-scratch build.
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

## Newer, larger items

Tracked separately once this list is confirmed and sequenced — covers the
`MershLab/harness` ports (ACP adapter, MCP server via `mcp_serve.py`,
`claude-code-subagent`, Docker sandbox recipe, `mini_swe_runner.py`, the
broader `plugins/` surface) and the net-new asks (universal external-agent
spawner for Claude Code/Codex/Cursor/Gemini CLI, universal model/MCP
connector, a ComfyUI-style visual stack builder), all discussed 2026-08-30
but not yet added here pending confirmation on sequencing.
