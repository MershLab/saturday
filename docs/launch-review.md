# Launch review — August 29, 2026

Three review passes over the repo at v0.8.0, one day before launch prep freeze:
a **senior-engineering security/correctness review**, a **backend↔frontend wiring audit**, and a
**VC-lens + design review**. Every P1 and all high-value P2 findings were **fixed in the same
pass** (see "Disposition"); what remains is listed as honest follow-up work.

## 1. Engineering review (security & correctness)

**Verdict: ship-ready.** The auth architecture (loopback bind + Host pinning + Origin checks +
SameSite=Strict cookie + constant-time compares), the tamper-evident session store, and the
fail-closed defaults are genuinely strong — better than most ships we review. Four P1s were found
and fixed:

| # | Finding | Severity | Disposition |
|---|---|---|---|
| 1 | Newline smuggling: `check_command` matched allow/deny rules against whitespace-folded text, so `git status\nsudo …` inherited a saved `git status*` rule and skipped the dangerous-command ask; deny rules could be evaded the same way | P1 | **Fixed** — rule matching now operates on per-line probes of the raw command; multiline commands never inherit allow suppression; deny rules match any contained line; the approval dialog shows the raw multiline command. Tests: `tests/test_security_review_r2.py` |
| 2 | `safety=off` bypassed the hardline floor (`rm -rf /`, `mkfs`, fork bomb); GNU long flags (`rm --recursive --force /`) evaded the patterns; pattern 1 also over-matched ANY absolute path (`rm -rf /tmp/cache` was unconditionally blocked) | P1 | **Fixed** — hardline binds in every mode; long flags fold to short before scanning; root pattern tightened to true root/system dirs, with normal-path recursive `rm` correctly demoted to guardrail-ask friction |
| 3 | Token accepted from the URL query (`?k=`) on every endpoint → leaks into history/Referer/titles | P1 | **Fixed** — one-shot cookie bootstrap on `GET /?k=` (Set-Cookie + `location.replace`), all other paths require header/cookie; client no longer appends tokens to `/api/file` URLs |
| 4 | `DELETE /api/sessions/all` unlinked transcripts while busy runtimes could still append (zombie sessions) | P1 | **Fixed** — same busy-wait discipline as single-session delete |

P2s fixed in the same pass: malformed `Content-Length` now 400s instead of killing the connection;
`/yolo` no longer flips safety mode on the shared base config (per-agent override, plan-mode
pattern); `.env` writes are `chmod 600` on POSIX; LLM client redirects strip `Authorization`
cross-host; the Telegram gateway replies to a stranger chat exactly once (liveness oracle closed).

Not fixed, accepted with rationale:
- Handler auth state is class-level (`Handler.token`): two `AppServer` instances in one process
  would clobber each other — only realistic in tests/embedding; noted for the 0.9 cycle.
- Shell tools inherit the full parent environment; `sandboxed=true` refuses to pretend isolation
  exists. The Docker sandbox recipe (roadmap) is the real fix.
- Empty-response loop nudges (`[empty response] Continue`) can burn the step budget on a truncated
  reasoning model; a distinct `finish_reason=length` stop reason would be kinder. Watch telemetry.

## 2. Wiring audit (backend ↔ frontend)

36 frontend API calls, 40+ backend routes: **every JS call resolves to a real route; no orphan
interactive elements**. The audit found and we fixed:

- **Critical:** `openSession()` referenced `meta` before its `const` declaration — a TDZ
  ReferenceError that silently broke startup session restore, live re-attach to busy sessions, and
  edit-&-resend/branch. One-line move, e2e-verified.
- A dropped NDJSON stream could leave the composer stuck in "working" forever — streams now
  recover: re-check server busy state, re-attach if the run is genuinely live, else release the UI.
- `send()` crashed on `state.info == null` (init failure) instead of routing to onboarding.
- `/api/runs` polls had no sequence guard (stale-over-fresh render race).
- Collapsed-sidebar scrim had no click handler (dead dismiss gesture).
- Dictation clobbered text typed while the mic was open.
- `/help` on the web rendered a wide ASCII table that turned to rubble in the narrow chat column —
  now a structured command grid (22 commands, aligned, wrapping descriptions).

Known dead surface, deliberately left: `GET /api/metrics` (usage ships in `/api/state`; the
endpoint is a future range-selector hook) and `POST /api/hooks` (hooks persist via `/api/config`;
the standalone endpoint is redundant). Static assets are served without the token — public files,
no secrets; API routes are all guarded.

Backend capabilities with no web surface, judged intentional (CLI/agent-side tooling):
`exporter.py` (dataset pipeline), `eval/` runner, `ablation.py` (test harness today — deserves an
`eval` flag or a home under `eval/` later), prompt-injection scanner (always-on guard).

## 3. VC lens

**What would make a partner lean in:**
- **Timing:** "harness" is now mainstream vocabulary (Open Interpreter repositioned around it);
  the heavy-platform backlash (Docker/RAM complaints) is loud and unmet.
- **Moat-shaped features:** tamper-evident audit chains + provenance marking (EU AI Act / GB
  45438-2025) is a compliance wedge no competitor touches; trajectory-export-for-training is a
  genuine DeepSeek-lineage differentiator with a natural data flywheel (runs → datasets → better
  fine-tunes).
- **Zero-dependency distribution** is the anti-OpenHands position: `pip install saturday`, no
  container, runs on a laptop.

**What the diligence will probe (be ready):**
1. **A published benchmark number.** Every credible agent cites SWE-bench-verified. Even a modest,
   honest score with a reproducible harness (`saturday eval`) beats nothing.
2. **Why does an agent harness become a business?** The open-core answer must be concrete:
   team/enterprise surface (audit bundles, approval policies, SSO), hosted evals, or the data
   flywheel. Decide and put one paragraph in the README.
3. **Model-provider dependence:** zero-dep + any-model is the pitch; OpenRouter/DeepSeek cost
   curves are the tailwind. Show cost-per-task numbers (roadmap) — they attack the market's
   loudest complaint.
4. **Retention evidence:** sessions/eval-usage telemetry is local-first (privacy-safe pitch), so
   plan an opt-in anonymous counter or GitHub-driven proof of activity.

## 4. Design review

**Strengths (keep):** coherent terminal-minimal identity; 22 themes including full light mode and
Omarchy parity; three-pane layout scales from 1440px down to mobile with a working scrim; the
workbench tabs (Activity/Changes/Files/Runs/Plan/Preview) expose real backend state, not decoration;
keyboard-first affordances (palette, `/` commands, Y/A/N approvals) match the power-user audience.

**Fixed in this pass:** the `/help` rubble (structured grid), scrim dead zone, approval dialog
truthfulness (raw multiline commands now visible), stuck-busy spinner.

**Recommended before GA (design debt, not blockers):**
1. **Brand mark:** the Σ glyph reads generic; commission a distinct logomark and use it in the
   titlebar, hero, and favicons (icons are currently generated).
2. **Empty-state balance:** the hero wastes ~40% vertical space at 1440×900; tighten the vertical
   rhythm or surface the workspace/file context there.
3. **Workspace path wrapping** in the Workbench info card breaks mid-word (`harne/ss`); use
   `overflow-wrap: anywhere` only on the path span or abbreviate the middle (`…Documents\harness`).
4. **Tab ambiguity:** the composer's `plan` mode chip and the workbench `Plan` tab share an
   accessible name — rename the chip's aria-label ("plan mode") to disambiguate for screen readers.
5. **Light-theme density:** the light theme is correct but 1–2px heavier visually; audit border
   opacity variables for the light palette.

## 5. Disposition summary

- Tests: **698 passed, 1 xfailed** at freeze (689 pre-existing + 16 new regression tests and
  contract updates).
- Lint: `ruff check .` clean.
- E2E: 11/11 Playwright scenarios pass (auth bootstrap, chat streaming, settings, themes,
  projects); CI's offline demo + doctor verified locally (a `doctor --offline` flag was added so
  CI's harness check no longer requires a running provider).
- Docs: this review + `docs/competitive-2026-08.md` added; CHANGELOG updated.

## 6. Independent pre-launch audit — round 2 (2026-08-29, later)

A second full pass (fresh eyes): exhaustive backend↔frontend wiring diff, v0.8 changelog feature
parity verification, live UI walkthrough (desktop + mobile + light theme + settings), and repo
hygiene. Result: **all v0.8 features verified wired end-to-end** (provenance, verify hooks,
approval memory, metrics, init, export compression, @-mentions, edit-&-resend, Ctrl+F, journal
restore, cost display, custom commands, schedules, 19 Omarchy themes, context panel, onboarding,
cross-chat search, Telegram, pywebview desktop). All 61 frontend API calls map to real backend
routes with matching methods, field names and query params; all stream event types match 1:1.

Fixed in this round:

| # | Finding | Fix |
|---|---|---|
| 1 | `/favicon.ico` (legacy probe) returned a JSON 404 | Route now serves the SVG icon (`webui.py`) |
| 2 | Release workflow could publish to PyPI while the installer matrix was red | `pypi` job now `needs: build` |
| 3 | Subagent `start` event carried a description the UI ignored (rows stuck at "starting…") | Rows now render the child task description (`app.js`) |
| 4 | Phantom `state.info.session_id` read in `togglePlanMode` | Removed dead read |
| 5 | Stale `src/deepforge` pycache from the rename survived on disk | Deleted (was untracked) |
| 6 | README drift: Providers section listed 5 of 16 providers; Layout claimed "30 offline tests" (actual: ~700); `saturday /metrics` invalid shell syntax; Layout tree missing 7 modules | All corrected |
| 7 | Phone-width first load: expanded sidebar covered the whole viewport | Sidebar defaults collapsed ≤900px unless the user explicitly pinned it open |
| 8 | Opening an empty stored session rendered a blank thread | Welcome hero + suggestions now show for empty sessions |
| 9 | Activity/Changes/Preview/Plan tabs rendered nothing when empty | Contextual empty-state hints, removed automatically when content arrives |

Verified unchanged (no action needed): Enter-to-send wiring (a test-harness key-delivery artifact
was run down and excluded), 409 conflict handling on secondary endpoints (generic toast already
surfaces the backend message), `warning` stream event (deliberate forward-compat in the protocol
vocabulary), test-only `/api/metrics` + `/api/hooks` endpoints (documented API surface).

Final state at freeze: **698 passed, 1 xfailed; ruff clean; live app verified on desktop, mobile
and light themes.**
