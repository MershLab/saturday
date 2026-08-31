from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from typing import Callable

from saturday.compress import compress
from saturday.agent.memory import TokenMeter, WorkingMemory, estimate_message_tokens, estimate_tokens
from saturday.llm.client import LLMClient, LLMContextOverflow, ModelResponse, StreamEvent
from saturday.prompts.templates import render_tool_response, split_reasoning
from saturday.tools.base import ToolRegistry
from saturday.types import Step, ToolResult, Trajectory

MEMORY_NUDGE_TEXT = (
    "[reminder] Before continuing: have you learned anything durable in this "
    "session worth keeping past it (a user preference, a project decision, a "
    "fact you'd otherwise have to re-derive)? If so, persist it now via the "
    "`memory` tool rather than letting it age out with this transcript."
)
# per-step parallel tool-call cap (Claude Code-class harnesses allow large
# batches; 16 covers realistic multi-edit steps without unbounded fan-out)
MAX_TOOL_CALLS_PER_STEP = 16
# tool results are compressed onto the wire at this size; larger outputs belong
# in spill files (shell already does this) rather than eating the context window.
# Compression, not a head cut: the end of a build log or test run is where the
# verdict is, and slicing kept the opening and dropped exactly that.
TOOL_RESULT_MAX_CHARS = 48_000

# a reply consisting ONLY of echoed tool-result block(s) is noise, not an answer
_ECHOED_TOOL_RESPONSE_RE = re.compile(
    r"(?:<tool_response>[\s\S]*?</tool_response>\s*)+"
)


@dataclass
class LoopHooks:
    on_step_start: Callable[[int], None] | None = None
    on_reasoning_delta: Callable[[str], None] | None = None
    on_text_delta: Callable[[str], None] | None = None
    on_tool_result: Callable[[ToolResult], None] | None = None
    on_compaction: Callable[[str], None] | None = None
    pre_tool_call: Callable[[str, dict], str | None] | None = None
    post_tool_call: Callable[[ToolResult], None] | None = None
    on_checkpoint: Callable[[list[dict]], None] | None = None

    def merge(self, other: "LoopHooks") -> "LoopHooks":
        merged = LoopHooks()
        for field in (
            "on_step_start",
            "on_reasoning_delta",
            "on_text_delta",
            "on_tool_result",
            "on_compaction",
            "pre_tool_call",
            "post_tool_call",
            "on_checkpoint",
        ):
            mine = getattr(self, field)
            theirs = getattr(other, field)
            if mine is None:
                setattr(merged, field, theirs)
            elif theirs is None:
                setattr(merged, field, mine)
            else:
                setattr(merged, field, _chain(mine, theirs))
        return merged


def _chain(first: Callable, second: Callable) -> Callable:
    def chained(*a, **k):
        r1 = first(*a, **k)
        if r1 is not None:
            return r1
        return second(*a, **k)

    return chained


def _content_parts(content) -> list:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    return list(content or [])


def _merge_content(a, sep: str, b):
    """Merge two message contents; tolerates vision part-lists (list content)."""
    if isinstance(a, str) and isinstance(b, str):
        return ((a or "") + sep + (b or "")).strip()
    merged = _content_parts(a) + _content_parts(b)
    return merged or None


def enforce_message_invariants(history: list[dict]) -> list[dict]:
    """hermes-agent rules: never two assistant/user in a row; only tool role repeats."""
    out: list[dict] = []
    for m in history:
        role = m.get("role")
        prev_role = out[-1].get("role") if out else None
        if role == prev_role == "assistant":
            prev_calls = out[-1].get("tool_calls") or []
            calls = m.get("tool_calls") or []
            out[-1] = dict(out[-1])
            out[-1]["tool_calls"] = prev_calls + calls
            if m.get("content"):
                out[-1]["content"] = _merge_content(out[-1].get("content"), "\n", m["content"])
            continue
        if role == prev_role == "user":
            out[-1] = dict(out[-1])
            out[-1]["content"] = _merge_content(out[-1].get("content"), "\n\n", m.get("content"))
            continue
        out.append(m)
    return out


class AgentLoop:
    def __init__(
        self,
        client: LLMClient,
        registry: ToolRegistry,
        *,
        max_steps: int = 200,
        temperature: float = 0.6,
        top_p: float = 0.95,
        max_tokens: int = 8192,
        compact_above_tokens: int = 60_000,
        max_parallel_tools: int = 4,
        memory: WorkingMemory | None = None,
        hooks: LoopHooks | None = None,
        keep_reasoning_in_history: bool = False,
        summarizer: Callable[[str], str] | None = None,
        max_run_tokens: int = 0,
        max_wall_seconds: int = 0,
        max_run_cost_usd: float = 0.0,
        cost_provider: str = "",
        cost_model: str = "",
        memory_nudge_interval: int = 0,
        tool_call_timeout: float | None = None,
        injection_guard: bool = True,
    ) -> None:
        self.client = client
        self.registry = registry
        self.max_steps = max_steps
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens
        self.compact_above_tokens = compact_above_tokens
        self.max_parallel_tools = max_parallel_tools
        self.memory = memory or WorkingMemory()
        self.hooks = hooks or LoopHooks()
        self.keep_reasoning_in_history = keep_reasoning_in_history
        self.summarizer = summarizer
        self.injection_guard = bool(injection_guard)
        # hard spend policy: stop_reason="budget" when cumulative tokens cross
        self.max_run_tokens = int(max_run_tokens or 0)
        # wall-clock cap: stop_reason="wall_clock" when elapsed time crosses
        self.max_wall_seconds = int(max_wall_seconds or 0)
        # dollar-denominated sibling of max_run_tokens: stop_reason="cost_budget"
        self.max_run_cost_usd = float(max_run_cost_usd or 0.0)
        self.cost_provider = cost_provider
        self.cost_model = cost_model
        # 0 = off; re-surfaces the memory-persistence reminder every N steps
        self.memory_nudge_interval = int(memory_nudge_interval or 0)
        # per-tool-call watchdog (None => wait forever); a hung tool must not
        # wedge the whole run since parallel results are gathered synchronously
        self.tool_call_timeout = float(tool_call_timeout) if tool_call_timeout else None
        # hermes parity: the LAST PROVIDER-REPORTED prompt_tokens. Compaction
        # decisions prefer this actual over projections (the estimate only
        # leads until the first response arrives). Restored on resume via
        # meter_state so long tasks stay calibrated across restarts.
        self.last_prompt_tokens = 0
        self.meter = TokenMeter()

    @property
    def meter_state(self) -> dict:
        return {
            "ratio": self.meter.ratio,
            "samples": self.meter.samples,
            "last_prompt_tokens": self.last_prompt_tokens,
        }

    def set_meter_state(self, state: dict | None) -> None:
        if not isinstance(state, dict):
            return
        try:
            self.meter.ratio = float(state.get("ratio") or 1.0)
            self.meter.samples = int(state.get("samples") or 0)
            self.last_prompt_tokens = int(state.get("last_prompt_tokens") or 0)
        except (TypeError, ValueError):
            pass

    def run(
        self,
        system_prompt: str,
        task: str,
        initial_history: list[dict] | None = None,
        attachments: list[str] | None = None,
    ) -> Trajectory:
        traj = Trajectory(task=task, system_prompt=system_prompt)
        if initial_history:
            # Checkpoint-resumed history may carry non-standard reasoning
            # fields (see _strip_think with keep=True); providers reject
            # unknown message fields, so strip them from the copies we build
            # here while the persisted copies on disk keep theirs.
            history: list[dict] = [
                _strip_reasoning_keys(dict(m)) for m in initial_history
            ]
            if attachments:
                from saturday.tools.vision import build_vision_content

                history.append({"role": "user", "content": build_vision_content(self._compose_task(task), attachments)})
            else:
                history.append({"role": "user", "content": task})
            traj.seed_user_message = dict(history[-1])
        elif attachments:
            from saturday.tools.vision import build_vision_content

            composed = self._compose_task(task)
            history = [{"role": "user", "content": build_vision_content(composed, attachments)}]
            traj.seed_user_message = dict(history[0])
        else:
            history = [{"role": "user", "content": self._compose_task(task)}]
            traj.seed_user_message = dict(history[0])

        stall_key: tuple | None = None
        stall_count = 0
        run_started_at = time.monotonic()
        for step_index in range(self.max_steps):
            # every attention event this step produces is tagged with it, so a
            # watcher can scrub back through where the agent went
            try:
                from saturday import attention

                attention.set_step(step_index)
            except Exception:
                pass
            if self.hooks.on_step_start:
                self.hooks.on_step_start(step_index)

            if self.memory_nudge_interval and step_index > 0 and step_index % self.memory_nudge_interval == 0:
                # a static "remember to persist facts" line in the system
                # prompt only gets read once; long runs need it re-surfaced
                # or durable facts age out with the transcript unnoticed.
                # Must land here, before this step's own request is built -
                # appending after assistant.to_openai() but before its tool
                # results would split an assistant/tool_call pair, which
                # strict OpenAI-compatible backends reject outright.
                history.append({"role": "user", "content": MEMORY_NUDGE_TEXT})

            if self.max_wall_seconds and time.monotonic() - run_started_at >= self.max_wall_seconds:
                last_text = next(
                    (s.assistant.content for s in reversed(traj.steps) if s.assistant.content),
                    None,
                )
                traj.final_answer = (
                    f"[budget stop] wall-clock limit {self.max_wall_seconds}s reached before the "
                    "goal completed." + (f" Last output: {last_text}" if last_text else "")
                )
                traj.stop_reason = "wall_clock"
                self._emit_checkpoint(history)
                return traj

            est_prompt_tokens = estimate_tokens(system_prompt) + sum(
                estimate_message_tokens(m) for m in history
            )
            # Compaction signal, hermes-style: prefer the provider's own last
            # reported prompt size once one exists (it lags by the newest tool
            # results but never mis-calibrates); before that, fall back to the
            # calibrated projection of this request.
            prompt_tokens = self.last_prompt_tokens or self.meter.project(est_prompt_tokens)
            if prompt_tokens > self.compact_above_tokens:
                self._compact(history)
                self.last_prompt_tokens = 0  # history changed: actuals are stale now
                if self.hooks.on_compaction:
                    self.hooks.on_compaction("history compacted")

            response: ModelResponse | None = None
            for overflow_attempt in range(2):
                try:
                    response = self._chat(system_prompt, history)
                    break
                except LLMContextOverflow:
                    if overflow_attempt > 0:
                        raise
                    self._compact(history, force=True)
                    if self.hooks.on_compaction:
                        self.hooks.on_compaction("compacted after context overflow")
            assert response is not None
            assistant = response.message

            if assistant.usage is not None:
                self.meter.observe(est_prompt_tokens, assistant.usage.prompt_tokens)
                if assistant.usage.prompt_tokens > 0:
                    # the model just told us the true prompt size: this becomes
                    # the primary compaction signal for the NEXT step
                    self.last_prompt_tokens = int(assistant.usage.prompt_tokens)

            traj.usage.add(assistant.usage or _zero())

            if not assistant.tool_calls:
                # A terminal answer must survive a budget that was exceeded
                # DURING this response: the spend already happened, so throwing
                # the finished answer away only destroys work the user paid
                # for. Budget interruption below applies only when there are
                # still tool calls waiting to run.
                reasoning, content = split_reasoning(assistant.content or "")
                final = content.strip()
                # Confused turns happen on small local models: the reply is a
                # verbatim echo of the tool result block and nothing else —
                # noise, not an answer. Treat it like an empty response.
                if final and _ECHOED_TOOL_RESPONSE_RE.fullmatch(final):
                    final = ""
                # A response the provider truncated at max_tokens ("length")
                # must not be mistaken for a terminal answer: thinking models
                # (qwen3, deepseek-r1) can burn the whole budget mid-thought
                # and never emit their tool call, so the truncated plan text
                # would end the run as a bogus "done". Nudge and continue.
                if final and response.finish_reason != "length":
                    traj.steps.append(Step(index=step_index, assistant=assistant))
                    traj.final_answer = final
                    traj.stop_reason = "done"
                    # Persist the completed exchange, not just the history
                    # that was sent into the final model call. Resume prefers
                    # this checkpoint, so omitting the terminal assistant turn
                    # makes the next prompt look like the previous turn never
                    # received an answer.
                    history.append(_strip_think(assistant.to_openai(), keep=self.keep_reasoning_in_history))
                    self._emit_checkpoint(history)
                    return traj
                history.append(_strip_think(assistant.to_openai(), keep=self.keep_reasoning_in_history))
                nudge = (
                    "[response truncated at the token limit] Continue from where you stopped: "
                    "emit your tool call or final answer now."
                    if response.finish_reason == "length"
                    else "[empty response] Continue pursuing the goal."
                )
                history.append({"role": "user", "content": nudge})
                continue

            if self.max_run_tokens and traj.usage.total_tokens >= self.max_run_tokens:
                last_text = next(
                    (s.assistant.content for s in reversed(traj.steps) if s.assistant.content),
                    None,
                )
                traj.final_answer = (
                    f"[budget stop] token budget {self.max_run_tokens} reached before the goal "
                    "completed." + (f" Last output: {last_text}" if last_text else "")
                )
                traj.stop_reason = "budget"
                self._emit_checkpoint(history)
                return traj

            if self.max_run_cost_usd and self.cost_provider and self.cost_model:
                from saturday.usage import estimate_cost_usd

                spent = estimate_cost_usd(self.cost_provider, self.cost_model, traj.usage.prompt_tokens, traj.usage.completion_tokens)
                # unpriced model (spent is None): never blocks, "never a
                # fake number" applies to enforcement too, not just display
                if spent is not None and spent >= self.max_run_cost_usd:
                    last_text = next(
                        (s.assistant.content for s in reversed(traj.steps) if s.assistant.content),
                        None,
                    )
                    traj.final_answer = (
                        f"[budget stop] cost budget ${self.max_run_cost_usd:.2f} reached (~${spent:.2f} spent) "
                        "before the goal completed." + (f" Last output: {last_text}" if last_text else "")
                    )
                    traj.stop_reason = "cost_budget"
                    self._emit_checkpoint(history)
                    return traj

            executed = assistant.tool_calls[:MAX_TOOL_CALLS_PER_STEP]
            # stall detector: three consecutive steps issuing the exact same
            # tool calls means the model is spinning, not progressing (2026
            # convergence: loop detection + step caps). Abort BEFORE running
            # the calls again — no tokens spent on the doomed repetition.
            key = tuple((c.name, json.dumps(c.arguments, sort_keys=True, default=str)) for c in executed)
            if executed and key == stall_key:
                stall_count += 1
            else:
                stall_key, stall_count = key, 1
            if stall_count >= 3:
                traj.final_answer = (
                    f"[stall] repeated the identical tool call {executed[0].name!r} in 3 consecutive "
                    "steps — aborting to avoid a loop. Change strategy, ask the user, or split the task."
                )
                traj.stop_reason = "stall"
                self._emit_checkpoint(history)
                return traj
            results = self._execute_calls(executed)
            serialized = _strip_think(assistant.to_openai(), keep=self.keep_reasoning_in_history)
            if len(assistant.tool_calls) > len(executed):
                serialized["tool_calls"] = [tc.to_openai() for tc in executed]
            history.append(serialized)
            step_tool_messages: list[dict] = []
            # images from ALL tool results in this step are batched into ONE
            # user relay AFTER the last tool message: a user message wedged
            # between tool results violates the provider invariant that a
            # tool_calls assistant turn is answered by its tool messages
            # contiguously (history shape must be tool,tool,...,user)
            step_images: list[tuple[str, list]] = []
            for call, result in zip(executed, results):
                if self.hooks.post_tool_call:
                    try:
                        self.hooks.post_tool_call(result)
                    except Exception:
                        pass
                if self.hooks.on_tool_result:
                    self.hooks.on_tool_result(result)
                # truncate the PAYLOAD before wrapping: slicing the rendered
                # string would cut off the closing </tool_response> tag exactly
                # on the largest outputs, degrading hermes-protocol parsing.
                # (The wrapper adds only ~50 chars, well under the headroom.)
                payload = result.output if result.ok else (result.error or result.output)
                if self.injection_guard:
                    try:
                        from saturday.prompt_injection import sanitize_tool_result

                        payload, _flagged = sanitize_tool_result(payload)
                    except Exception:
                        pass  # guard must never break the loop
                tool_msg = {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": render_tool_response(
                        call.name, result.ok, compress(payload, TOOL_RESULT_MAX_CHARS)),
                }
                history.append(tool_msg)
                step_tool_messages.append(dict(tool_msg))
                if result.images:
                    step_images.append((call.name, result.images))
            if step_images:
                try:
                    from saturday.tools.vision import build_vision_content

                    names = ", ".join(name for name, _ in step_images)
                    images = [img for _, imgs in step_images for img in imgs]
                    vision_msg = {
                        "role": "user",
                        "content": build_vision_content(f"[images from tools {names}]", images),
                    }
                    history.append(vision_msg)
                    step_tool_messages.append(dict(vision_msg))
                except Exception:
                    pass
            traj.steps.append(
                Step(index=step_index, assistant=assistant, results=list(results), tool_messages=step_tool_messages)
            )
            self._emit_checkpoint(history)

            if any(call.name == "finish" for call in executed):
                traj.final_answer = next(
                    (c.arguments.get("answer", "") for c in assistant.tool_calls if c.name == "finish"),
                    "",
                )
                traj.stop_reason = "done"
                return traj

        last_text = next(
            (s.assistant.content for s in reversed(traj.steps) if s.assistant.content),
            None,
        )
        traj.final_answer = last_text or ""
        traj.stop_reason = "max_steps"
        return traj

    def _emit_checkpoint(self, history: list[dict]) -> None:
        if self.hooks.on_checkpoint is None:
            return
        try:
            self.hooks.on_checkpoint([dict(m) for m in history])
        except Exception:
            pass

    def _execute_calls(self, calls):
        blocked: dict[int, ToolResult] = {}
        runnable: list[tuple[int, object]] = []
        for i, call in enumerate(calls):
            if self.hooks.pre_tool_call:
                try:
                    block = self.hooks.pre_tool_call(call.name, call.arguments)
                except Exception as exc:
                    # fail closed: a crashing hook must never degrade into a
                    # silent allow — the tool stays blocked and the model sees
                    # why
                    block = f"hook error (fail-closed): {exc}"
                if block is not None:
                    blocked[i] = ToolResult(call_id=call.id, name=call.name, ok=False, output="", error=block)
                    continue
            runnable.append((i, call))

        results: dict[int, ToolResult] = dict(blocked)
        if runnable:
            calls_by_index = {i: c for i, c in runnable}
            # no context manager: __exit__ calls shutdown(wait=True), which
            # would block until a HUNG tool actually finishes and make the
            # watchdog bound nothing. wait=False abandons the worker instead
            # (it leaks until process exit; the run itself proceeds).
            # tools run on a pool, and the pool starts with no run context, so
            # every event a tool raised reported step 0 and no session at all
            try:
                from saturday import attention as _attn

                _ctx = _attn.snapshot()

                def _execute(call_id, name, arguments):
                    _attn.restore(_ctx)
                    return self.registry.execute(call_id, name, arguments)
            except Exception:
                _execute = self.registry.execute
            pool = ThreadPoolExecutor(max_workers=min(self.max_parallel_tools, len(runnable)))
            try:
                futures = {pool.submit(_execute, c.id, c.name, c.arguments): i for i, c in runnable}
                # one shared deadline so N queued results can't multiply the
                # worst-case wall time by N
                timeout = self.tool_call_timeout
                deadline = (time.monotonic() + timeout) if timeout else None
                for fut, i in futures.items():
                    try:
                        results[i] = fut.result(
                            timeout=(deadline - time.monotonic()) if deadline else None
                        )
                    except FuturesTimeoutError:
                        # the hung call is abandoned; its thread dies with the process
                        call = calls_by_index[i]
                        results[i] = ToolResult(
                            call_id=call.id,
                            name=call.name,
                            ok=False,
                            output="",
                            error=f"(tool timed out after {timeout:g}s)",
                        )
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
        return [results[i] for i in range(len(calls))]

    def _compose_task(self, task: str) -> str:
        mem = self.memory.render()
        parts = [f"# Goal\n{task}"]
        if len(self.memory):
            parts.append(f"# Working memory (pinned facts & decisions)\n{mem}")
        return "\n\n".join(parts)

    def _chat(self, system_prompt: str, history: list[dict]) -> ModelResponse:
        # Last-line defense before the wire: nothing non-standard reaches the
        # provider even if a hook injected reasoning fields into live history
        # mid-run. Copy-on-write, so checkpointed history keeps its fields.
        messages = [{"role": "system", "content": system_prompt}] + [
            _strip_reasoning_keys(m) for m in enforce_message_invariants(history)
        ]

        def cb(evt: StreamEvent) -> None:
            if evt.type == "reasoning" and self.hooks.on_reasoning_delta:
                self.hooks.on_reasoning_delta(evt.reasoning_delta)
            elif evt.type == "text" and self.hooks.on_text_delta:
                self.hooks.on_text_delta(evt.delta_text)

        return self.client.chat(
            messages,
            tools=self.registry.specs() or None,
            temperature=self.temperature,
            top_p=self.top_p,
            max_tokens=self.max_tokens,
            stream_callback=cb if self.hooks.on_text_delta or self.hooks.on_reasoning_delta else None,
        )

    def _compact(self, history: list[dict], force: bool = False) -> None:
        keep_tail = 6
        cut = len(history) - (2 if force else keep_tail)
        if cut <= 0:
            return
        while cut > 0 and history[cut].get("role") == "tool":
            cut -= 1
        overflow = history[:cut]
        if not overflow:
            return
        goal_verbatim = ""
        for m in overflow:
            if m.get("role") == "user" and "# Goal" in str(m.get("content") or ""):
                goal_verbatim = str(m["content"])
                break

        digest_lines: list[str] = []
        for m in overflow:
            role = m.get("role")
            if role == "user":
                text = str(m.get("content") or "").strip()
                if not text.startswith("[context was compacted"):
                    digest_lines.append(f"user said: {text[:400]}")
            elif role == "assistant":
                calls = ", ".join(
                    tc.get("function", {}).get("name", "?") for tc in m.get("tool_calls") or []
                )
                text = (m.get("content") or "").strip()
                entry = f"assistant said: {text[:400]}" + (f" | called: {calls}" if calls else "")
                digest_lines.append(entry)
            elif role == "tool":
                name = m.get("name") or "tool"
                content = str(m.get("content"))[:300]
                digest_lines.append(f"{name} -> {content}")

        transcript_excerpt = "\n".join(digest_lines)[-8000:]
        summary: str | None = None
        if self.summarizer is not None and transcript_excerpt.strip():
            try:
                summary = self.summarizer(transcript_excerpt)[:6000] or None
            except Exception:
                summary = None
        if summary is None:
            # Structured fallback (hermes context-engine parity): a flat dump
            # forces the model to re-derive state; sections let it resume
            # directly. Deterministic extraction only — no extra LLM call.
            import json as _json

            files_modified: list[str] = []
            decisions: list[str] = []
            decision_rx = re.compile(
                r"\b(decid(e|ed|ing)|chose|chosen|will use|switch(ed)? to|pivoted)\b", re.IGNORECASE
            )
            for m in overflow:
                for tc in m.get("tool_calls") or []:
                    name = tc.get("function", {}).get("name")
                    if name not in ("write_file", "edit_file"):
                        continue  # reads are noise under "Files modified"
                    try:
                        fn_args = _json.loads(tc.get("function", {}).get("arguments") or "{}")
                    except Exception:
                        continue
                    path = str(fn_args.get("path") or "")
                    if path and path not in files_modified:
                        files_modified.append(path)
                text = str(m.get("content") or "").strip()
                role = m.get("role")
                if role == "assistant" and text and decision_rx.search(text[:400]):
                    decisions.append(text[:200])
            template = ["[COMPACTED CONTEXT]"]
            if digest_lines:
                template += ["## Progress", *(f"- {line}" for line in digest_lines[-40:])]
            if decisions:
                template += ["## Decisions", *(f"- {d}" for d in decisions[-5:])]
            if files_modified:
                template += ["## Files modified", *(f"- {f}" for f in files_modified[-10:])]
            template += ["", "Continue pursuing the original goal below."]
            summary = "\n".join(template)

        head_block = ""
        if goal_verbatim:
            head_block = f"# Goal (preserved verbatim)\n{goal_verbatim}\n\n"

        # Pin the summary (and only what it doesn't already carry) into working
        # memory: it must survive FUTURE compactions, unlike the compacted user
        # message which will itself be digested away next round.
        pinned = summary or ""
        excerpt_tail = transcript_excerpt[-2000:]
        if transcript_excerpt and excerpt_tail not in pinned:
            pinned = (pinned + "\n" + excerpt_tail).strip()
        self.memory.add("compaction-summary", pinned)
        tail = history[cut:]
        del history[:]
        history.extend(
            [
                {
                    "role": "user",
                    "content": (
                        "[context was compacted to stay within budget]\n"
                        f"{head_block}"
                        f"Summary of earlier work:\n{summary}\n\n"
                        "Continue pursuing the original goal."
                    ),
                }
            ]
            + tail
        )


def _strip_think(openai_msg: dict, keep: bool = False) -> dict:
    content = openai_msg.get("content")
    if isinstance(content, str) and ("<think>" in content or "<scratch_pad>" in content):
        reasoning, remainder = split_reasoning(content)
        if keep and reasoning:
            out = dict(openai_msg, content=remainder or None)
            out["reasoning_content"] = reasoning
            return out
        return dict(openai_msg, content=remainder or None)
    return openai_msg


def _zero():
    from saturday.types import Usage

    return Usage()


# Non-standard reasoning fields we persist for replay/UI but must never send
# on the wire (providers reject unknown message fields).
_REASONING_ONLY_KEYS = ("reasoning", "reasoning_content")


def _strip_reasoning_keys(msg: dict) -> dict:
    """Copy-on-write removal of reasoning-only fields; returns msg untouched
    (same object) when there is nothing to strip."""
    if not any(k in msg for k in _REASONING_ONLY_KEYS):
        return msg
    return {k: v for k, v in msg.items() if k not in _REASONING_ONLY_KEYS}
