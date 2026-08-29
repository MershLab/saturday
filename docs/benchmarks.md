# Saturday benchmarks

How we measure the harness, and what measuring it has already paid for.
Every number on this page names its model, hardware, and exact reproduction
commands; what we don't claim is stated as plainly as what we do. Companion
docs: `BENCHMARKING.md` (the runbook for SWE-bench Verified / Terminal-Bench),
`scripts/agent_bench.py` (the pinned suite this page describes).

**Headline so far:** the pinned suite found and fixed five real agent-loop
robustness gaps against live models — the kind that silently fail tasks on
half-compliant models and are nearly invisible in manual testing. Each fix is
regression-tested. That is the suite doing its job: harness bugs surface as
mechanical case failures, not vibes.

## 1. Pinned agentic suite (`saturday-pinned-agentic-v1`)

**What it measures: harness quality** — can an agent drive Saturday's tool
loop (files, shell, search, multi-step state) to mechanically-verified end
states — on any model, including small local ones. 16 cases, each in an
isolated temp workspace; verifiers inspect final file/command state only,
never the narrative. The suite is validated in both directions before every
run (`scripts/_validate_bench.py`: zero verifier passes on untouched fixtures,
zero fails on hand-solved states).

| Property | Value |
|---|---|
| Cases | 16 (file ops ×4, editing ×4, shell/python ×3, search ×2, multi-step/precision ×3) |
| Verifier | binary per case (composite cases require every part) |
| Unattended contract | `safety_mode=autonomous` in disposable workspaces; hardline blocklist active |
| Trajectories | saved per case with provenance stamps (GB 45438-2025 / EU AI Act) |

**Role in the project:** this is the pre-release regression gate
(`BENCHMARKING.md`: "internal regression safety: `saturday eval` green + a
pinned slice before each release") and the absorption test-bed that keeps
Saturday honest against the messy reality of local and non-native
tool-calling models. Published cross-model scores land here as the funded
runs in §2 complete.

### Local-model validation run — ollama / qwen2.5-coder:7b, `num_ctx=32768` (RTX 3060 Laptop 6 GB, Windows)

Recorded 2026-08-29, suite `saturday-pinned-agentic-v1`, `max_steps=12`,
`max_run_tokens=150000`, python 3.14.2. This machine's ollama intermittently
fails GPU discovery (server log) and degrades toward CPU speed; wall-clock is
therefore an upper bound. The `400` rows are instant ollama-side request
rejections (same binary succeeds on retry in isolation) — recorded as ERROR,
not retried into the score.

| Run | Harness state | Pass rate | Mean reward | Tokens | Wall | Character of failures |
|---|---|---|---|---|---|---|
| A | tags ignored (`<tool_call>` only) | 3/16 (18.8%) | 0.188 | 275k | 18 min | agents answered with plans, never called tools |
| B | + bare-JSON & `<tool_response>` absorption | 4/16 (25.0%) | 0.250 | 275k | 19 min | agents engage; 7B fails to converge on multi-edit tasks |
| C | + trailing-JSON & echo-noise fixes (final) | 3/16 (18.8%) | 0.188 | 383k | 18 min | full-budget engagement (12-step runs); 2 cases lost to the ollama 400 (one of them passed in run B) |

Per-case, run C: PASS `file_read_extract`, `search_count_md`, `no_tool_precision`;
FAIL elsewhere with genuine agentic engagement (3–12 steps, up to 54k tokens
per case, stall-detector and max-steps terminations); ERROR
`script_create_run`, `search_find_string` (ollama 400).

**Reading:** the differences between runs are within model variance plus the
ollama 400 flake — the honest claim is **~19–25% for a 7B local coder model on
free hardware**, where the harness now gives the model every chance to act
(all four observed tool-call formats are absorbed) and the remaining failures
are model capability, not harness parsing. The suite's value tonight was not
the score; it was finding five real loop robustness gaps, each now fixed with
regression tests (see below).

Reproduce:

```sh
# ollama's default num_ctx for this model is 4096, which silently truncates
# Saturday's prompt (system + 26 tool schemas) — the model then never sees the
# tools. Create a 32k-context variant first:
printf 'FROM qwen2.5-coder:7b\nPARAMETER num_ctx 32768\n' > Modelfile
ollama create qwen2.5-coder-32k -f Modelfile
python scripts/agent_bench.py --provider ollama --model qwen2.5-coder-32k \
    --out eval_runs/agent-bench-final --max-steps 12 --tag "qwen2.5-coder-32k"
```

### Results — ollama / qwen3:4b (partial, 1 case + smoke rows)

qwen3:4b with default thinking is a poor fit for agentic tool use at this
size: on `file_write_exact` it emitted reasoning + a plan as content and ended
its turn without any tool call (the harness correctly read that as a terminal
answer; `reward=0`). Generation on a 6 GB laptop GPU is also slow enough
(~2–14 min/case) that a full default-thinking row costs hours for a
predictably low score. Row intentionally not completed; if you want it:

```sh
python scripts/agent_bench.py --provider ollama --model qwen3:4b \
    --out eval_runs/agent-bench-qwen3-4b --max-steps 16 \
    --extra-instruction "/no_think" --tag "qwen3:4b no-think"
```

### Harness bugs this suite caught on local models (all fixed, regression-tested)

1. **Token-limit truncation treated as a final answer** — a thinking model
   that exhausts `max_tokens` mid-thought returned truncated plan text with no
   tool call; the loop accepted it as `stop_reason=done`. Fixed: truncated
   responses stay in history with a continuation nudge
   (`tests/test_truncation_continue.py`).
2. **Bare-JSON tool calls ignored** — qwen2.5-coder via ollama emits tool
   calls as a whole-message JSON document with no `<tool_call>` wrapper; the
   Hermes parser dropped it and the JSON blob became the "final answer".
   Fixed (`tests/test_bare_tool_json.py`).
3. **Calls wrapped in `<tool_response>` ignored** — the model imitates the
   protocol's response tag for its own call. Absorbed with a shape guard:
   our rendered results never carry an `arguments` key, so echoes cannot
   become phantom calls.
4. **Trailing bare JSON after a scratch pad ignored** — the most common
   pattern: `<scratch_pad>…</scratch_pad>` then the call as a JSON object
   ending the message. Absorbed only when the object ENDS the message;
   prose after it (a model discussing JSON) disqualifies.
5. **Echoed tool-response treated as a final answer** — a reply consisting
   only of `<tool_response>` block(s) is noise; the loop now nudges instead
   of ending the run as `done`.

### Local-model operational notes (measured, 2026-08-29)

- **ollama `num_ctx` default is fatal for agent prompts.** Saturday's system
  prompt + 26 tool schemas exceed 4096 tokens; ollama silently truncates the
  prompt middle, the model "loses" the tools, and answers in plain text.
  Always run local models behind a ≥16k-context variant
  (`PARAMETER num_ctx 32768`).
- **qwen3:4b, default thinking**: emits plan-only turns without tool calls on
  multi-step tasks at this size; `/no_think` is the sane setting for <8B
  qwen3 in agentic use.
- **qwen2.5-coder:7b**: follows the Hermes protocol's `<scratch_pad>` but
  skips the `<tool_call>` wrapper (bare JSON) — absorbed by the harness since
  commit `0cb4e1b`.
- **Windows + RTX 3060 (6 GB)**: ollama 0.18 intermittently fails GPU
  discovery and degrades to CPU-speed generation; expect minutes per agent
  step on this hardware class.

### Found by this suite (already fixed)

- **Token-limit truncation masquerading as a final answer** — thinking models
  (qwen3, deepseek-r1) can exhaust `max_tokens` mid-thought and return
  truncated plan text with no tool call; the loop accepted it as
  `stop_reason=done` and the task silently failed. Fixed in
  `agent/loop.py` (truncated responses now stay in history with a
  continuation nudge); regression tests in
  `tests/test_truncation_continue.py`. This is the suite doing its job:
  harness bugs surface as mechanical case failures, not vibes.

## 2. SWE-bench (Verified / Lite)

Status: **rig validated, funded run pending.** `scripts/swebench_runner.py`
follows the mini-swe-agent pattern (one Docker eval container per instance,
patch → `preds.json`, grading via `sb-cli` or the local harness). Validated on
2026-08-29: Docker daemon functional, `princeton-nlp/SWE-bench_Lite` loads
(300 instances, expected schema), runner CLI operational.

What remains is the expensive part: a working provider key and the token
budget (roughly 1–2M tokens per instance at high budgets — smoke first).
The runbook, including the unattended-mode contract and cost caps, is in
`BENCHMARKING.md` §1. We will publish % resolved + Wilson 95% CI and cost per
run; we will not publish a number from a smoke slice.

## 3. Terminal-Bench 2.x

Planned via the Harbor agent plugin (`BENCHMARKING.md` §2); required for
leaderboard visibility. Not started — needs the same funded key as §2.

## Honesty policy

- Every number on this page names its model, quantization, hardware, and
  exact command. No aggregated "Saturday scores X" claims across models.
- Harness-quality numbers (§1) are labeled as such and never presented as
  model-capability numbers.
- External benchmark numbers (§2/§3) are only published from full or
  clearly-labeled slice runs with spend recorded.
