# Saturday

**The zero-dependency agent harness.** Any model, every surface, provable trails — audit chains, approval gates, and training-ready trajectories — with nothing else to install.

A SOTA open agentic harness distilled from two lineages: DeepSeek's agent-harness research (explicit reasoning traces, verifiable rewards, trajectory export for RL/SFT) and Nous Research's Hermes agent & function-calling protocols. Zero-dependency core, Python 3.10+. Since v0.3: **MCP-native, durably checkpointed**. Since v0.4: **web search + text browser, vision attachments, skills learning loop**. Since v0.5: **computer use (background-safe), 16 providers, interactive console app**. Since v0.6: **context breakdown panel, 19 Omarchy themes, personal assistant mode, destructive-action guardrails with DB auto-backup, cross-chat search, onboarding wizard**. Since v0.8: **provenance marking (GB 45438-2025 / EU AI Act), post-edit verify hooks, per-action approval memory, usage metrics + /metrics, `saturday init`, export compression, competitive-parity UI — @-file mentions, edit-&-resend/branch-from-message, Ctrl+F in-chat search, per-edit journal restore, in-session cost, custom slash commands, schedules UI**.

**Why it's different:** `pipx install saturday` and you have a full agent — 26 tools, computer use, MCP, evals, four UIs (terminal, web, desktop, Telegram) — with no Docker, no Node, no 24 GB RAM requirement. Heavy agent platforms got heavy; Saturday stayed a tool you can read the entire source of in an afternoon.

```
           _                 _
 ___  __ _| |_ _   _ _ __ __| | __ _ _   _
/ __|/ _` | __| | | | '__/ _` |/ _` | | | |
\__ \ (_| | |_| |_| | | | (_| | (_| | |_| |
|___/\__,_|\__|\__,_|_|  \__,_|\__,_|\__, |
                                     |___/
```

## Why

Agent frameworks usually force a trade-off: heavy platforms vs toy loops. Saturday is a **harness-first** product — a small, auditable runtime that any reasoning model (DeepSeek-R1/V3, Hermes, Qwen, Llama) can drive, with the research-grade pieces built in:

| Capability | Origin | What you get |
|---|---|---|
| Reasoning traces | DeepSeek-R1 (`<think>`) + Hermes-3 (`<scratch_pad>`) | Parsed, streamed, stripped from history; preserved in trajectories |
| Verifiable rewards | DeepSeek-R1 GRPO-style checkers | Rule-based `Verifier`s + eval runner with pass-rate/reward summaries |
| Trajectory export for training | R1 distillation + hermes-agent batch generation | Every run saved as OpenAI-format messages JSONL — ready for SFT/RL |
| Everything is a plugin | deepseek-harness (`dsh`) | Plugins contribute tools + prompt persona; conflict-checked install |
| Goal tracking | dsh goal tools | `create_goal` / `get_goal` / `update_goal` with status machine |
| Background jobs | dsh job tools | `shell run_in_background=true` → `job_list` / `job_output` / `job_kill` |
| Concurrent tool exec | hermes-agent | ThreadPoolExecutor, order-preserving results |
| Message invariants | hermes-agent | Never two assistant/user turns in a row; only tool role repeats |
| Hook lifecycle | hermes-agent plugins | `pre_tool_call` (can block), `post_tool_call`, stream/thinking callbacks |
| Hermes XML protocol | Hermes-Function-Calling | `<tools>` catalog, `<tool_call>` parsing, `<tool_response>` wrapping, validate-retry hints |
| Context compaction | hermes-agent context engine | Threshold-triggered digest pinned into working memory |

## Computer use — including true background mode

Saturday can see and operate real Windows apps. Two modes, both stdlib-only:

**Foreground** (`pointer`, `keyboard`, `window`, `screen annotate=marked`): exact-coordinate mouse/keyboard control with accessibility-tree grounding, landmark memory, and inline safety approvals. Parity extras: multi-monitor capture (`screen display=N`), middle-click, and graceful window close (`window close=`) in both delivery modes.

**Background** (`--background` or `desktop_background_only: true`): the agent works while you keep working.
- `app_open` launches apps minimized **without stealing focus** (and auto-restores yours if Windows insists)
- `ui_invoke` presses buttons / fills text fields via UI Automation patterns — no cursor, no keystrokes, target stays occluded
- `ui_tree scope=win:<title>` + `screen capture_window=<title>` read background windows
- pointer/keyboard/focus are policy-blocked; a focus-guard restores your foreground window after every action

```sh
saturday run --detach --background "fill the expense report in Excel from receipts/"
Get-Content .saturday\bg\bg-<id>.log -Wait      # follow from anywhere
saturday chat --resume bg-<id>                   # inspect later
```

Verified live: drove Calculator (7×6=42) and Notepad entirely in the background while the user's foreground window handle stayed identical.

## Providers

16 built-in: `deepseek`, `openai`, `anthropic`, `google`, `nous`, `xai`, `mistral`, `groq`, `moonshot`, `qwen`, `zai`, `together`, `openrouter`, `azure-openai`, `ollama`, `vllm`. All OpenAI-compatible endpoints; per-provider default models, env-overridable base URLs, per-vendor auth per their docs (Bearer via `Authorization`, `api-key` header for Azure, deployment-path routing + `api-version` for Azure, the OpenAI-compat layer for Anthropic/Google). Non-native function-callers get the Hermes XML fallback automatically.

## Install

**Developer CLI (recommended)** — one global command, the way you'd install
`claude` or `codex`:

```sh
pipx install saturday      # or: uv tool install saturday / pip install saturday
saturday                   # start the REPL
saturday app               # launch the desktop web UI
```

Requires Python 3.10+ only. The core is stdlib-only (zero third-party
dependencies), so `pipx run saturday` also works without installing. Upgrades
via `pipx upgrade saturday`.

**Desktop app** — prebuilt per-OS installers from
[Releases](../../releases) (no Python or runtime needed; built for every push
of a `v*` tag):

| OS | Artifact |
|---|---|
| Windows 10/11 x64 | `Saturday-Setup-<ver>.exe` (per-user install, no admin) |
| macOS Apple Silicon | `Saturday-macos-arm64.dmg` |
| macOS Intel | `Saturday-macos-x86_64.dmg` |
| Debian/Ubuntu/Mint | `saturday_<ver>_<arch>.deb` |
| Fedora/RHEL/openSUSE | `saturday-<ver>-<arch>.rpm` |
| Arch Linux | `saturday-<ver>-any.pkg.tar.zst` (pacman -U, pure Python) |
| Any Linux | `saturday_<ver>_<arch>.AppImage` (chmod +x, double-click) |

Your data stays in `~/.saturday` (uninstaller keeps it). macOS builds are
ad-hoc signed — right-click → Open on first launch; Developer-ID
signing/notarization is a pending step. Intel Macs are legacy (Apple ended
support; CI x64 builds end Aug 2027) — `pipx` covers them afterwards.

**From source (any OS with Python 3.10+):**

```sh
git clone <this-repo> && cd harness
pip install -e .
saturday app        # desktop UI
```

No third-party dependencies. Works against any OpenAI-compatible endpoint.

**Rebuilding installers locally:** `powershell scripts/build_windows.ps1`
(NSIS; downloads a portable copy automatically if needed),
`bash scripts/build_linux.sh all`, `bash scripts/build_macos.sh`, or on Arch:
`makepkg` inside `packaging/arch/` (drop the wheel next to the PKGBUILD) —
or push a `v*` tag and GitHub Actions (`.github/workflows/release.yml`) builds
every desktop installer and publishes the CLI package to PyPI.

## Quickstart

```sh
saturday setup                         # one-time: provider + API key + model (connection-tested)
saturday config --show                 # provider/model/status
saturday run "write fizzbuzz.py and run it"
saturday chat                          # interactive session
saturday eval --out eval_runs          # verifiable suite
saturday export --out train.jsonl      # trajectories -> SFT dataset
```

Programmatic use:

```python
from saturday.agent import Agent
from saturday.config import AgentConfig

cfg = AgentConfig(provider="deepseek", model="deepseek-reasoner", max_steps=200)
agent = Agent(cfg=cfg)
traj = agent.run(
    "Refactor src/ to remove dead code, then run the test suite.",
    on_reasoning_delta=lambda s: print(s, end=""),
    on_text_delta=lambda s: print(s, end=""),
)
print(traj.final_answer, traj.usage.total_tokens)
```

Offline demo (no key needed):

```sh
python examples/offline_demo.py
```

## Providers

16 built-in: `deepseek` · `openai` · `anthropic` · `google` · `nous` · `xai` · `mistral` · `groq` · `moonshot` · `qwen` · `zai` · `together` · `openrouter` · `azure-openai` · `ollama` · `vllm` (self-hosted R1-distill etc.)
Set the matching `*_API_KEY`. All OpenAI-compatible endpoints; non-native tool-calling models automatically get the Hermes XML protocol instead of function-calling schemas.

## Tool toggles

Turn tools off globally or per session — by name or family:

```sh
saturday run --disable web,computer_use "summarize notes/"   # no network, no desktop
```

Families: `web` (search+fetch) · `browser` · `computer_use` (pointer/keyboard/ui/screen) · `shell` · `python` · `file_writes` · `subagents` · `memory`. In the app: Settings → Safety & approvals → Tool toggles. Per-session: `/toggle <name|family>` in chat; `/tools` shows what's active. Disabled tools are hidden from the model entirely — not just blocked after the fact.

## Tools

`shell` (+background jobs) · `read_file` · `write_file` · `edit_file` · `list_dir` · `glob` · `grep` · `python` (persistent REPL) · `web_fetch` · `todo` · `create/get/update_goal` · `job_list/output/kill` · `task` (subagents)

Workspace paths are sandboxed against escaping the root. Extend via plugins:

```python
from saturday.plugins import make_plugin
from saturday.agent import Agent

my_plugin = make_plugin("acme", [MyCrmTool(), MySqlTool()],
                        persona_sections=["# House style\nAlways cite ticket IDs."])
agent = Agent(cfg=cfg, plugins=[my_plugin])   # replaces default plugins
```

## Benchmarks & evals

Every number we publish, with model, hardware and exact reproduction commands: [docs/benchmarks.md](docs/benchmarks.md).
The runbook for SWE-bench Verified / Terminal-Bench is [BENCHMARKING.md](BENCHMARKING.md).

## Evals & training-data flywheel

```python
from saturday.eval import EvalRunner, EvalCase, file_created, contains_any

runner = EvalRunner(lambda: Agent(cfg), out_dir="eval_runs")
results = runner.run([EvalCase(id="t1", task="...", verifier=file_created("out.txt"))])
print(EvalRunner.summarize(results))
```

Every trajectory (messages, tool calls, reward, token usage) lands in `eval_runs/*.json`; merge them with `saturday export` into one JSONL for fine-tuning. `saturday export --compress 12000` shrinks older tool results to short omission markers (goal + recent turns stay verbatim) so SFT datasets stay token-lean. This is the compounding moat: **usage produces training data; evals gate quality.**

## Provenance & compliance

```sh
saturday config --set provenance_marking=visible   # metadata | visible | off
```

Exported trajectories, eval artifacts and audit bundles carry a machine-readable
`provenance` block (`ai_generated`, generator+version, provider/model, session id,
timestamp, content SHA-256) aligned with China's GB 45438-2025 labeling measures and
EU AI Act Art. 50. `visible` mode also appends a short disclosure footer to answers.

## Post-edit verification

```sh
saturday config --set verify_command="python -m pytest -q"
# or per-file: use {path} anywhere in the command
```

After every successful `write_file`/`edit_file`, Saturday runs your command and feeds
the output back to the agent inline — failing tests are corrected on the very next step.
(Python writes additionally get a free stdlib `ast` syntax check.)

## Project bootstrap

```sh
saturday init        # AGENTS.md template + .saturday/mcp.json.example + hooks example
saturday doctor      # preflight: key/provider/workspace/registry + local JSON validity
saturday run "/metrics"   # or /metrics inside chat: turns, completion rate, tokens/turn, outcomes
```

## MCP (Model Context Protocol)

Any stdio MCP server becomes a set of native Saturday tools — zero extra deps:

```json
{
  "servers": {
    "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]},
    "myserver":   {"command": "my_server.py"}
  }
}
```

```sh
saturday mcp            # handshake + list tools per server
saturday run "..."      # tools appear automatically; collisions alias as <server>_<tool>
```

## Durability

Every agent step snapshots the conversation atomically to
`<home>/sessions/<id>.checkpoint.json` — plus the agent's brain: pinned working
memory, todo plan, active goal, and the file-journal position (all fsync'd).
Kill the process anywhere; resume exactly mid-trajectory with plan and memory
intact. `/rewind [n]` rolls workspace FILES back to a checkpoint state
(Cursor-style file+conversation checkpointing); `saturday audit` verifies the
tamper-evident session chain:

```sh
saturday run --session build-42 "big task"
saturday chat --resume build-42
```

## Platform surfaces (v0.5)

```sh
saturday tui                          # alt-screen console: header, status bar, telemetry
saturday serve --port 8787            # POST /message {"text": "..."} -> JSON answer (bearer token required; --token/--no-token)
saturday gateway --token $T --allow 42  # Telegram bot, per-chat sessions, long-polling (--allow is mandatory; --allow-all at your own risk)
```

Optional extras:
```sh
pip install 'saturday[desktop]' && saturday ...   # pillow screen-capture fallback
pip install 'saturday[browser]' && playwright install chromium   # JS-rendered web_browser_js tool
```

The `screen` tool captures your display and attaches it for vision inspection — on Windows it uses a built-in PowerShell/.NET capture, no dependencies. The `web_browser_js` tool executes JavaScript via headless Chromium and can return full-page screenshots as vision images.

## Safety notes

- Shell/python tools execute real commands locally. Defense is layered, in precedence order: **hardline blocklist** (rm -rf /, mkfs, fork bomb...) → **deny rules** → **background-only structural gating** (foreground pointer/keyboard blocked) → **reserved auth scopes** (governance asks even with safety off) → **destructive-action guardrails** → **dangerous-pattern ask/deny** → scope tiers.
- Classification is textual — variable indirection or encoded payloads can evade it. For unattended use run inside a container and set `SATURDAY_SANDBOXED=1`: an isolated executor then *replaces* pattern friction structurally (hardline blocks, deny rules and reserved scopes still apply).
- `pre_tool_call` hooks return a string to block any call by policy.
- File tools refuse paths outside the workspace root.
- **Destructive-action guardrails** (on by default): DROP DATABASE/TABLE, TRUNCATE, recursive deletes, `git reset --hard`, and SQL DELETE/UPDATE without WHERE ask for confirmation **even when safety is off**, and block when no approver exists. Disable via Settings or `SATURDAY_GUARDRAILS=0`.
- **Database auto-backup**: destructive shell commands referencing `*.db` / `*.sqlite*` files snapshot them into `<workspace>/.saturday/backup/` first (last 10 kept, 64 MB cap per file).
- **Write verification**: Python files written/edited are syntax-checked (stdlib `ast`) and the agent is warned inline so it self-corrects.
- **SSRF guard**: `web_fetch` / `browser` / `web_search` refuse loopback, private, link-local, and cloud-metadata addresses (including across redirects). Override for local endpoints with `SATURDAY_ALLOW_LOCAL_FETCH=1`.
- **Project trust gate**: a repo's `.env` and `.saturday/mcp.json` are only honored after you approve that project once (or set `SATURDAY_TRUST_ALL_PROJECTS=1`); non-interactive runs skip them.
- **Privileged writes blocked**: the agent cannot `write_file`/`edit_file` `.env` or `.saturday/mcp.json`, so a prompt-injected model can't persist its own provider/MCP config changes (edit those by hand).
- **Network surfaces fail closed**: `serve` requires a per-launch bearer token and pins Host/Origin to loopback; the Telegram gateway refuses to start without `--allow <chat_ids>` (or an explicit `--allow-all`); the web app pins Host/Origin even when launched with `--no-token`.

## Desktop app extras (v0.6)

- **Context panel**: click the token meter (or `/context`) for a per-section breakdown - system prompt, tool schemas, user/assistant/tool messages, images - against the compaction threshold; updates live each step.
- **Themes**: 19 themes generated from the official Omarchy Linux palettes (Tokyo Night, Catppuccin, Gruvbox, Nord, Kanagawa, Rose Pine, Flexoki Light, ...) plus the Saturday dark/light pair; theme menu on the toolbar, "system" follows the OS.
- **Personal assistant mode**: full capability with a plain-language surface — it acts on your PC end-to-end (computer use defaults to **background-first**: apps launch minimized, windows are driven without stealing your mouse/keyboard), hides commands/step-counters behind friendly status lines, and reports outcomes like a person. Give it a name and let it address you however you like (JARVIS-style), with an optional hands-free voice loop: speak, it sends itself, it speaks back. Toggle in Settings > General or run `saturday chat --assistant`.
- **Per-project memory**: project chats get their own `.saturday/MEMORY.md` inside the project workspace, layered over global memory.
- **Cross-chat search**: Ctrl+K palette searches message content across every saved session.
- **Onboarding wizard**: first launch offers provider + API key setup, stored locally in `~/.saturday/.env`.

## Desktop app extras (v0.8 — competitive-parity round)

A UI/UX audit of 22 agent products (Claude Code, Codex, Cursor, Cline, Devin,
OpenHands, Goose, Warp, Amp, Zed, Replit, …) surfaced the recurring surfaces
Saturday lacked; all of them are now built in:

- **@-file mentions**: type `@` in the composer for a workspace-file autocomplete.
- **Edit & resend / Branch from here / Retry**: every user message can fork the
  conversation at that point (Goose/OpenHands-style history surgery — the fork
  keeps the exact raw-message prefix, original session untouched).
- **Find in chat**: Ctrl+F highlight-all search with match count and navigation.
- **Per-edit restore**: Changes tab gets *undo last edit* plus a restore-history
  panel backed by the file journal (Cline/Roo-style checkpoints, per edit).
- **Cost display**: per-turn and running session cost estimates on the stats
  line and token meter (list-price table; unknown models stay silent).
- **Custom slash commands**: save prompt templates as `/name` commands
  (`$ARGS` substitution) in Settings → Commands — a local prompt library.
- **Schedules**: cron automations managed in Settings → Schedules and fired by
  the app's built-in watcher (`SATURDAY_SCHEDULE_WATCHER=0` to opt out).
- **Compact now**: one-click context compaction from the context panel.
- **Approval flash**: the window title pulses when an approval waits while
  you're in another window.
- **Export as HTML**: shareable self-contained transcript next to MD/JSON export.
- **Per-turn 👍/👎**: local reward ratings (`feedback.jsonl`) feeding the
  training-data flywheel.

### Round 2 (same audit, second pass)

- **Runs monitor**: a stage tab listing every session with live running/stopping/idle
  status, model, uptime and per-run stop buttons — the parallel-agents panel
  the Warp/Cursor/Codex class of tools standardized.
- **Queue management**: queued messages drag-reorder and click-to-edit.
- **Plan approval**: the Plan tab offers *Approve plan & switch to Act*.
- **"Fix this"**: failed tool cards carry a one-click repair action.
- **Review + attach changes**: ask the agent to review its own edits; attach
  any change's diff into the composer; open current file contents.
- **Git status chip**: read-only branch/changed/+/− chip on the Changes tab.
- **Journal compare**: before/after diff preview per journal entry before restoring.
- **Session archive** with a sidebar *show archived* toggle.
- **Model favorites**: star models, Alt+M cycles them, Ctrl+M opens the menu.
- **Live app preview**: embed a dev-server URL (sandboxed iframe, remembered
  per session) beside the screenshot stream.

### Round 3 (interactive core)

- **`ask_user` tool**: the agent can stop and ask you a question with
  one-click options (Lovable/Windsurf parity); it waits for your answer and
  proceeds on timeout — even in plan mode, since it mutates nothing.
- **Deny with feedback**: attach a note when refusing an approval and the
  agent sees it inline ("use X instead of Y") instead of blindly retrying.
- **AI session titles**: fresh chats rename themselves with a 3-6 word title
  after the first reply (one tiny background call; off-switch in Settings).
- **Live subagent progress**: `task` children stream their steps and tool
  results as rows nested under the parent card (Claude Code / Warp parity).
- **Prompt enhancer**: a wand in the composer rewrites your draft into a
  sharper prompt; click again to undo.
- **Per-chat model override**: the model menu switches models for the current
  chat only when one is open (Cline/Amp parity).

### Round 4 (common-sense UX)

- **Switch sessions mid-run**: start a new chat or open another session while
  one is still working — the run continues and stays watchable in Runs;
  re-opening it re-attaches to the live stream mid-turn, approvals included.
- **Plan & Safety sit with the composer** (Cursor/Cline placement): Plan mode
  is finally visible and toggleable from the UI, and Safety opens an explicit
  menu with plain-language descriptions instead of a misclick-prone cycle.
- **Esc stops the agent**; the composer hint tells you what's possible right
  now (queue a follow-up / answer Y-A-N / Esc to stop).
- **Settings gear in the sidebar footer**, composer refocus when a run ends,
  Esc clears the session filter, and narrow windows shed header badges
  gracefully.

## Layout

```
src/saturday/
├── agent/        loop, memory, todo, facade
├── llm/          zero-dep OpenAI-compatible client (SSE streaming, retries, 16 providers)
├── prompts/      system builder, ChatML/Hermes templates
├── tools/        registry + builtin tools (files/shell/python/web/computer use/goals/jobs)
├── eval/         verifiers + runner + builtin suite + export compression
├── plugins.py    everything-is-a-plugin assembly
├── webui.py      desktop app surface (local HTTP API + event stream)
├── gateway.py    Telegram bot surface
├── safety.py     layered approval/scope/guardrail engine
├── sessions.py   tamper-evident session store + checkpoints
├── mcp_client.py Model Context Protocol client (stdio servers -> native tools)
├── provenance.py GB 45438-2025 / EU AI Act provenance marking
└── cli.py        run/chat/eval/tools/config/export/init/audit/doctor/...
tests/            ~715 offline tests (scripted model, no network)
```

## License

MIT
