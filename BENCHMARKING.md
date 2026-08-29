# Benchmarking Saturday

Feature parity is self-graded; benchmark numbers are not. This doc covers the
three benchmarks that matter for "better than Claude Code / Cursor" claims,
with Saturday-specific configuration. Rule of thumb for credibility:
**SWE-bench Verified** for issue-fixing, **Terminal-Bench 2.x** for
real-terminal agentic work (and leaderboard visibility), Aider polyglot only
as a raw-model proxy.

## Unattended-mode contract (applies to ALL benchmarks)

Benchmarks run headless, so every human gate must be structurally disabled:

| Setting | Why |
|---|---|
| `SATURDAY_SANDBOXED=1` | inside the eval container: structural isolation replaces pattern friction |
| `SATURDAY_GUARDRAILS=0` | guardrails fail closed with no approver — they would block legitimate fixes (`git reset --hard`, recursive deletes) |
| provider API key env | `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` / ... passed into the container |
| `--ci` / `saturday run` | one-shot mode with exit codes; no REPL prompts |
| `--disable web,browser,computer_use,memory,subagents` | hide tools the container can't use; fewer distractions, fewer tokens |
| `--max-run-tokens <N>` | hard spend ceiling per instance |

## 1. SWE-bench Verified (500 real GitHub issues)

The reference pattern for custom harnesses is [mini-swe-agent](https://mini-swe-agent.com/latest/usage/swebench/)
(>74% with a 100-line bash-only agent — proof the scaffold, not exotic tooling,
is what's being measured).

This repo ships `scripts/swebench_runner.py`: one Docker eval container per
instance, agent edits `/testbed`, patch captured to `preds.json`.

```sh
pip install datasets                      # runner-only dependency
python -m swebench.harness.prepare_images # OR: --image-source ghcr (prebuilt)

# smoke test (2 instances) before any real run
python scripts/swebench_runner.py --limit 2

# full run, 4 workers, spend-capped
python scripts/swebench_runner.py --workers 4 --max-run-tokens 4000000
```

Grade (pick one):

```sh
# free cloud evaluation (fastest)
sb-cli submit swe-bench_verified test \
  --predictions_path runs/<run_id>/preds.json --run_id <run_id>

# local harness (needs docker + disk for per-instance images)
python -m swebench.harness.run_evaluation \
  --dataset_name princeton-nlp/SWE-bench_Verified \
  --predictions_path runs/<run_id>/preds.json \
  --max_workers 4 --run_id <run_id>
```

Reporting convention: % resolved + Wilson 95% CI. Cost scale note: a full
Verified run is roughly 1-2M tokens/instance at high budgets — always smoke
first and record $ per run.

## 2. Terminal-Bench 2.x via Harbor (leaderboard-visible)

[Terminal-Bench](https://www.tbench.ai) grades end-state tests in containers;
it is explicitly agent-agnostic. Custom agents plug in as a Harbor plugin:

1. `uv tool install "harbor[daytona]"` and `harbor auth login`
2. Implement an agent class (see the community template
   [vipulgupta2048/terminal-bench-docker](https://github.com/vipulgupta2048/terminal-bench-docker)):

   ```python
   from harbor.agents.base import BaseInstalledAgent

   class SaturdayAgent(BaseInstalledAgent):
       @classmethod
       def name(cls) -> str: return "saturday"
       def version(self) -> str: return "0.9.0"
       def setup(self, ...):  # upload ~/.saturday auth or write env file
       def create_run_agent_commands(self, instruction: str, ...) -> list[str]:
           b64 = base64.b64encode(instruction.encode()).decode()  # escaping-proof
           return [f'echo {b64} | base64 -d > /task.md && '
                   f'unbuffer saturday run --ci --disable web,browser,computer_use '
                   f'"$(cat /task.md)"']
   ```

3. Config YAML: `network_mode: host` (container needs your API endpoint),
   `override_timeout_sec: 600+`, wrap the CLI in `unbuffer` if it checks TTY.
4. Smoke test ONE task before committing to the ~17h full suite:

   ```sh
   uv run harbor run -c my-config.yaml --dataset-task-names hello-world
   ```

5. Official leaderboard submission requires ≥5 trials/task, uploaded publicly,
   then a PR against `harbor-framework/terminal-bench-2-1` (CI validates +
   maintainers review trajectories). Community submissions open/close in
   windows — check the repo before planning a submission.

## 3. Aider polyglot (model proxy only)

`aider/benchmark/benchmark.py` drives aider's own `Coder` classes — there is
no supported way to substitute an external agent like Saturday. Two honest
options:

- Run polyglot against your chosen MODEL through aider directly
  (`./benchmark/benchmark.py <name> --model <m> --edit-format whole --threads 10
  --exercises-dir polyglot-benchmark`) as a model-capability datapoint.
- Treat Saturday's own `saturday eval` suite + SWE-bench/Terminal-Bench as
  the harness-quality numbers instead.

## What beats what

| Claim | Requires |
|---|---|
| "matches Claude Code" | ≥ Claude Code's published SWE-bench Verified number on the SAME underlying model class, same trial count |
| "beats Cursor" | Terminal-Bench 2.x leaderboard row above Cursor's agent, or a head-to-head on identical tasks with n≥5 trials |
| internal regression safety | `saturday eval` green + a pinned 20-instance SWE-bench slice before each release |
