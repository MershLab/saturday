from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from saturday.types import Message, ToolCall, Usage


@dataclass
class ModelResponse:
    message: Message
    finish_reason: str | None = None
    raw: dict[str, Any] | None = None


@dataclass
class StreamEvent:
    type: str
    delta_text: str = ""
    reasoning_delta: str = ""
    tool_call: ToolCall | None = None


class LLMError(RuntimeError):
    pass


class LLMContextOverflow(LLMError):
    """Classified per hermes error_classifier: retry after compressing context."""

    pass


# Upper bound on server-supplied backoff: a broken/hostile Retry-After header
# (hours, days) must not stall the agent loop past our own bounded policy.
_RETRY_AFTER_MAX_SECONDS = 120


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Default urllib redirect handling re-sends every header — including
    Authorization — to the redirect target. A misconfigured or compromised
    base_url must not leak provider API keys cross-host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            old_host = urllib.parse.urlsplit(req.full_url).netloc
            new_host = urllib.parse.urlsplit(new.full_url).netloc
            if old_host.lower() != new_host.lower():
                for sensitive in ("Authorization", "Cookie", "X-Api-Key"):
                    new.headers.pop(sensitive, None)
        return new


_OPENER = urllib.request.build_opener(_SafeRedirectHandler)


def classify_error(exc: Exception) -> tuple[str, int]:
    """Return (kind, retry_after_seconds). kind in auth|rate_limit|context_overflow|server|network."""
    status = getattr(exc, "code", None)
    headers = getattr(exc, "headers", None) or {}
    retry_after = 0
    ra = headers.get("Retry-After") if hasattr(headers, "get") else None
    if ra is not None:
        try:
            retry_after = max(0, int(float(ra)))
        except (TypeError, ValueError):
            try:
                from email.utils import parsedate_to_datetime
                import time as _t

                delta = parsedate_to_datetime(ra).timestamp() - _t.time()
                retry_after = max(0, int(delta))
            except Exception:
                retry_after = 0
    # Cap the honored value: min(our parse, ceiling) so a single bad header
    # can never dominate the retry schedule.
    retry_after = min(retry_after, _RETRY_AFTER_MAX_SECONDS)
    body = _error_body(exc)
    haystack = f"{getattr(exc, 'msg', '')} {body}".lower()
    if status == 429:
        return "rate_limit", retry_after
    if status in (401, 403):
        return "auth", 0
    if _is_overflow_text(haystack):
        return "context_overflow", 0
    if isinstance(exc, LLMContextOverflow):
        return "context_overflow", 0
    if status is not None and 400 <= status < 500 and status != 408:
        return "bad_request", 0
    if status is not None and status >= 500:
        return "server", retry_after
    return "network", 0


# Generic phrases every OpenAI-compatible backend tends to use; providers can
# add their own via ProviderProfile.overflow_markers.
GENERIC_OVERFLOW_MARKERS = ("context length", "maximum context", "too many tokens")


def _error_body(exc: Exception) -> str:
    # HTTPError.read() drains the response: a second call returns b"". Cache
    # the decoded body on the exception so classify_error() and any later
    # marker checks observe the SAME text — otherwise provider overflow
    # markers never match and the compact-and-retry path is skipped. Also
    # mirror the body onto .body so getattr(exc, "body", "") consumers get
    # the real content instead of "".
    cached = getattr(exc, "_cached_body", None)
    if isinstance(cached, str):
        return cached
    resp = getattr(exc, "read", None)
    if callable(resp):
        try:
            body = resp().decode("utf-8", errors="replace")[:2000]
        except Exception:
            body = ""
    else:
        raw = getattr(exc, "body", "")
        body = raw if isinstance(raw, str) else ""
    for attr in ("_cached_body", "body"):
        try:
            setattr(exc, attr, body)
        except Exception:
            pass  # exception types with __slots__ can't carry attributes
    return body


def _redact(text: str, secret: str | None) -> str:
    """Keep the API key out of error strings that may surface in logs/UI."""
    if secret and secret in text:
        return text.replace(secret, "***")
    return text


def _is_overflow_text(text: str, extra_markers: tuple[str, ...] = ()) -> bool:
    for marker in GENERIC_OVERFLOW_MARKERS + extra_markers:
        if marker.lower() in text:
            return True
    return False


def _extract_json_object(text: str) -> dict[str, Any]:
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fence.group(1) if fence else text
    start = candidate.find("{")
    if start == -1:
        raise ValueError("no JSON object found")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(candidate)):
        ch = candidate[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(candidate[start : i + 1])
    raise ValueError("unbalanced JSON object")


# loop.py caps native tool calls per step; the bare-JSON path mirrors that
# cap locally (importing loop here would be circular)
_MAX_BARE_TOOL_CALLS = 16

_HERMES_TOOL_RE = re.compile(
    r"<tool_call>\s*(?P<body>\{.*?\}|.*?)\s*</tool_call>", re.DOTALL
)

# Weaker models sometimes wrap their own call in the protocol's RESPONSE tag
# (imitating the <tool_response> blocks they saw). Our rendered responses
# carry {"name", "content"/"error"} and never "arguments"/"parameters", so
# absorbing call-shaped JSON from these blocks cannot misfire on echoes.
_HERMES_RESPONSE_TOOL_RE = re.compile(
    r"<tool_response>\s*(?P<body>\{.*?\})\s*</tool_response>", re.DOTALL
)


def _tool_call_from_obj(obj: Any, require_args: bool = False) -> ToolCall | None:
    if not isinstance(obj, dict):
        return None
    name = obj.get("name", "")
    args = obj.get("arguments") or obj.get("parameters") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {"_raw": args}
    if not name or not isinstance(name, str):
        return None
    if require_args and "arguments" not in obj and "parameters" not in obj:
        # response/result payloads also carry a "name" (next to content/error);
        # only true calls carry an arguments/parameters key
        return None
    return ToolCall(name=name, arguments=args if isinstance(args, dict) else {"_raw": args})


def _bare_tool_json(text: str) -> list[ToolCall]:
    """Detect a tool call the model emitted as a bare JSON document — no
    <tool_call> wrapper at all. Half-compliant local models (qwen2.5-coder
    via ollama) do this routinely: they output exactly
    `{"name": ..., "arguments": {...}}` (or a list of those) as the whole
    message. A chat reply that happens to be exactly one tool-call-shaped
    JSON object with no other text is not a plausible final answer, so the
    false-positive risk is negligible next to silently ending the run."""
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return []
    if isinstance(obj, dict):
        call = _tool_call_from_obj(obj, require_args=True)
        return [call] if call else []
    if isinstance(obj, list):
        calls = [_tool_call_from_obj(o, require_args=True) for o in obj[:_MAX_BARE_TOOL_CALLS]]
        return [c for c in calls if c]
    return []


def _extract_last_json_object(text: str, start: int) -> tuple[Any, int] | None:
    """String-aware scan for the first balanced JSON object at or after
    `start`; returns (obj, end_index_exclusive) or None."""
    depth = 0
    in_str = False
    esc = False
    obj_start = -1
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_start != -1:
                try:
                    return json.loads(text[obj_start : i + 1]), i + 1
                except json.JSONDecodeError:
                    return None
    return None


_TRAILING_CALL_HEAD = re.compile(r'\{\s*"name"\s*:')


def _trailing_tool_json(text: str) -> list[ToolCall]:
    """Absorb a call-shaped JSON object that ENDS the message (only
    whitespace after it). This is how scratch-pad models emit calls when they
    drop the <tool_call> wrapper: `<scratch_pad>...</scratch_pad>\\n{"name":
    ..., "arguments": ...}`. Prose after the object disqualifies it — that is
    a model discussing JSON, not issuing a call."""
    m: re.Match | None = None
    for m in _TRAILING_CALL_HEAD.finditer(text):
        pass  # keep the LAST candidate — earlier objects may be quoted examples
    if m is None:
        return []
    found = _extract_last_json_object(text, m.start())
    if found is None:
        return []
    obj, end = found
    if text[end:].strip():
        return []
    call = _tool_call_from_obj(obj, require_args=True)
    return [call] if call else []


def parse_hermes_tool_calls(text: str) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for rx, require_args in ((_HERMES_TOOL_RE, False), (_HERMES_RESPONSE_TOOL_RE, True)):
        for m in rx.finditer(text):
            body = m.group("body").strip()
            try:
                obj = json.loads(body)
            except json.JSONDecodeError:
                try:
                    obj = _extract_json_object(body)
                except (ValueError, json.JSONDecodeError):
                    continue
            call = _tool_call_from_obj(obj, require_args=require_args)
            if call:
                calls.append(call)
    if not calls:
        # no <tool_call> tags anywhere: accept a bare tool-call JSON document,
        # or a call-shaped object ending the message after a scratch pad
        calls = _bare_tool_json(text) or _trailing_tool_json(text)
    return calls


@dataclass
class _PartialCall:
    id: str = ""
    name: str = ""
    args_buf: str = ""


_TOOL_CALL_OPEN = "<tool_call>"


class _HermesStreamFilter:
    """Gates streamed text so raw <tool_call> XML never reaches the UI.

    Why: the non-stream path extracts Hermes tool calls BEFORE emitting, but
    streaming deltas used to pass through raw, leaking literal XML to live
    rendering. Algorithm: chunks append to a tiny buffer and we emit the
    longest prefix that provably cannot participate in the open tag.
    - A complete "<tool_call>" in the buffer flips suppression on permanently;
      everything from it onward is withheld (end-of-stream extraction recovers
      the calls from the full text, exactly like the non-stream path).
    - Otherwise any trailing '<' with fewer than len("<tool_call>") chars
      after it is held back until later chunks prove it is not a tag start.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._suppressing = False

    def feed(self, chunk: str) -> str:
        """Consume one delta; return the text safe to show now (maybe "")."""
        if self._suppressing:
            return ""
        self._buf += chunk
        open_idx = self._buf.find(_TOOL_CALL_OPEN)
        if open_idx != -1:
            # Suppress from the tag's first '<' onward; earlier text is safe.
            self._suppressing = True
            visible, self._buf = self._buf[:open_idx], ""
            return visible
        # Hold back a tail that could still grow into "<tool_call>": start at
        # the last '<' if fewer than len(tag) chars follow it. Any earlier '<'
        # is disproven already — either the full tag would be present (found
        # above) or the next char diverges from the literal tag.
        cut = len(self._buf)
        last_lt = self._buf.rfind("<")
        if last_lt != -1 and len(self._buf) - last_lt < len(_TOOL_CALL_OPEN):
            cut = last_lt
        visible, self._buf = self._buf[:cut], self._buf[cut:]
        return visible

    def finish(self) -> str:
        """Flush any held-back tail that turned out to be plain text."""
        if self._suppressing:
            self._buf = ""
            return ""
        visible, self._buf = self._buf, ""
        return visible


class LLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        model: str = "",
        timeout: float = 300.0,
        max_retries: int = 4,
        extra_headers: dict[str, str] | None = None,
        fallback_models: list[str] | None = None,
        overflow_markers: tuple[str, ...] | None = None,
        deployment_path: bool = False,
        api_version: str = "",
        sample_defaults: dict[str, float] | None = None,
        omit_sampling: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.extra_headers = extra_headers or {}
        self.fallback_models = fallback_models or []
        self.overflow_markers = tuple(overflow_markers or ())
        self.deployment_path = bool(deployment_path)
        self.api_version = api_version
        self.sample_defaults = dict(sample_defaults or {})
        self.omit_sampling = bool(omit_sampling)
        self.total_usage = Usage()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self.extra_headers)
        return headers

    def _endpoint_url(self, model: str | None = None) -> str:
        """Per-provider chat completions URL (Azure deviates from the flat path)."""
        base = self.base_url
        if not base:
            raise LLMError("base URL not configured (set the provider's base_url env)")
        if not self.deployment_path:
            return f"{base}/chat/completions"
        if base.endswith("/chat/completions"):
            url = base  # user supplied the full URL already
        else:
            # docs: {endpoint}/openai/deployments/{deployment}/chat/completions
            depl = urllib.parse.quote(model or self.model, safe="")
            url = f"{base}/openai/deployments/{depl}/chat/completions"
        if self.api_version and "api-version=" not in url:
            sep = "&" if "?" in url else "?"
            url += f"{sep}api-version={self.api_version}"
        return url

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.6,
        top_p: float = 0.95,
        max_tokens: int = 8192,
        stream_callback: Callable[[StreamEvent], None] | None = None,
        stop: list[str] | None = None,
    ) -> ModelResponse:
        # Provider doc-mandated sampling (e.g. DeepSeek reasoning wants 1.0).
        temperature = self.sample_defaults.get("temperature", temperature)
        top_p = self.sample_defaults.get("top_p", top_p)
        payload: dict[str, Any] = {
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": stream_callback is not None,
        }
        if not self.omit_sampling:
            # Gemini 3.7+ docs: temperature/top_p removed — never send them
            payload["temperature"] = temperature
            payload["top_p"] = top_p
        if not self.deployment_path:
            # Azure docs: the deployment is in the URL, the body omits "model"
            payload["model"] = self.model
        if tools:
            payload["tools"] = [{"type": "function", "function": t} for t in tools]
        if stop:
            payload["stop"] = stop
        if stream_callback is not None:
            # Strict OpenAI-compatible backends emit the terminal usage chunk
            # ONLY when asked; without it streamed usage stays zero and
            # token-budget stops never fire.
            payload["stream_options"] = {"include_usage": True}

        import random

        candidates = [self.model] + [m for m in self.fallback_models if m != self.model]
        last_err: Exception | None = None
        emitted = {"any": False}

        guarded_cb = stream_callback
        if stream_callback is not None:

            def guarded_cb(evt: StreamEvent) -> None:
                emitted["any"] = True
                stream_callback(evt)

        for candidate in candidates:
            if not self.deployment_path:
                payload["model"] = candidate
            body = json.dumps(payload).encode("utf-8")  # encode once per model
            for attempt in range(self.max_retries + 1):
                try:
                    if guarded_cb is not None:
                        return self._chat_stream(payload, guarded_cb, body=body, model=candidate)
                    return self._chat_once(payload, body=body, model=candidate)
                except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ConnectionError, OSError) as exc:
                    last_err = exc
                    kind, retry_after = classify_error(exc)
                    if kind != "context_overflow" and self.overflow_markers:
                        if _is_overflow_text(_error_body(exc).lower(), self.overflow_markers):
                            kind = "context_overflow"
                    # Targeted fallback: some strict backends reject the
                    # unknown "stream_options" field with an instant 400.
                    # Strip it and replay the identical request once (same
                    # model, no backoff sleep) rather than failing over.
                    if (
                        kind == "bad_request"
                        and "stream_options" in payload
                        and "stream_options" in _error_body(exc).lower()
                    ):
                        del payload["stream_options"]
                        body = json.dumps(payload).encode("utf-8")
                        continue
                    if kind == "auth":
                        raise LLMError(
                            f"{kind} error for model '{candidate}': {_redact(str(exc), self.api_key)}"
                        ) from exc
                    if kind == "context_overflow":
                        raise LLMContextOverflow(
                            f"context overflow on '{candidate}': {_redact(str(exc), self.api_key)}"
                        ) from exc
                    if kind == "bad_request":
                        break
                    if attempt >= self.max_retries:
                        break
                    if guarded_cb is not None and emitted["any"]:
                        raise LLMError(
                            f"stream emitted deltas before failing; not retrying to avoid duplicate output: "
                            f"{_redact(str(exc), self.api_key)}"
                        ) from exc
                    wait = retry_after or min(0.5 * (2**attempt) + random.uniform(0, 0.5), 20.0)
                    time.sleep(wait)
        raise LLMError(
            f"LLM request failed after retries and fallbacks: {_redact(str(last_err), self.api_key)}"
        ) from last_err

    def _post(self, payload: dict[str, Any], body: bytes | None = None, model: str | None = None) -> Any:
        req = urllib.request.Request(
            self._endpoint_url(model),
            data=body if body is not None else json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        with _OPENER.open(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _chat_once(self, payload: dict[str, Any], body: bytes | None = None, model: str | None = None) -> ModelResponse:
        data = self._post(payload, body=body, model=model)
        choice = (data.get("choices") or [{}])[0]
        usage_raw = data.get("usage") or {}
        usage = Usage(
            prompt_tokens=usage_raw.get("prompt_tokens", 0),
            completion_tokens=usage_raw.get("completion_tokens", 0),
            total_tokens=usage_raw.get("total_tokens", 0),
        )
        self.total_usage.add(usage)
        msg = Message.from_openai(choice.get("message") or {}, usage=usage)

        if not msg.tool_calls and isinstance(msg.content, str):
            hermes_calls = parse_hermes_tool_calls(msg.content)
            if hermes_calls:
                cleaned = _HERMES_TOOL_RE.sub("", msg.content).strip()
                msg.tool_calls = hermes_calls
                msg.content = cleaned or None
        return ModelResponse(message=msg, finish_reason=choice.get("finish_reason"), raw=data)

    def _chat_stream(self, payload: dict[str, Any], cb: Callable[[StreamEvent], None], body: bytes | None = None, model: str | None = None) -> ModelResponse:
        req = urllib.request.Request(
            self._endpoint_url(model),
            data=body if body is not None else json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        partials: dict[int, _PartialCall] = {}
        current_bucket = 0
        finish_reason: str | None = None
        usage = Usage()
        hermes_gate = _HermesStreamFilter()
        with _OPENER.open(req, timeout=self.timeout) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "text/event-stream" not in ctype:
                data = json.loads(resp.read().decode("utf-8"))
                choice = (data.get("choices") or [{}])[0]
                u = data.get("usage") or {}
                usage = Usage(
                    prompt_tokens=u.get("prompt_tokens", 0),
                    completion_tokens=u.get("completion_tokens", 0),
                    total_tokens=u.get("total_tokens", 0),
                )
                msg = Message.from_openai(choice.get("message") or {}, usage=usage)
                # servers that ignore stream:true return a plain JSON body; the
                # Hermes XML fallback must run here exactly like _chat_once.
                # Extraction happens BEFORE the text emission so clients never
                # see raw <tool_call> XML followed by duplicate tool-call
                # events (persisted history was already clean; this only
                # affected live rendering).
                if not msg.tool_calls and isinstance(msg.content, str):
                    hermes_calls = parse_hermes_tool_calls(msg.content)
                    if hermes_calls:
                        msg.tool_calls = hermes_calls
                        msg.content = _HERMES_TOOL_RE.sub("", msg.content).strip() or None
                if msg.content:
                    cb(StreamEvent(type="text", delta_text=msg.content))
                for tc in msg.tool_calls or []:
                    cb(StreamEvent(type="tool_call", tool_call=tc))
                self.total_usage.add(usage)
                return ModelResponse(message=msg, finish_reason=choice.get("finish_reason"))

            data_lines: list[str] = []
            undecoded_count = 0

            def dispatch_frame(frame: str) -> bool:
                """Handle one SSE data frame; False signals [DONE]/stop."""
                nonlocal finish_reason, current_bucket, undecoded_count
                text = frame.strip()
                if text == "[DONE]":
                    return False
                try:
                    evt = json.loads(text)
                except json.JSONDecodeError:
                    # Malformed/undecodable frame: skipping keeps the stream
                    # alive; there is no logging infra, so stay silent but
                    # keep counting so the behavior is explicit.
                    undecoded_count += 1
                    return True
                u = evt.get("usage")
                if u:
                    usage.prompt_tokens = max(usage.prompt_tokens, u.get("prompt_tokens", 0))
                    usage.completion_tokens = max(usage.completion_tokens, u.get("completion_tokens", 0))
                    usage.total_tokens = max(usage.total_tokens, u.get("total_tokens", 0))
                for choice in evt.get("choices") or []:
                    fr = choice.get("finish_reason")
                    if fr:
                        finish_reason = fr
                    delta = choice.get("delta") or {}
                    rc = delta.get("reasoning_content") or delta.get("reasoning")
                    if rc:
                        reasoning_parts.append(rc)
                        cb(StreamEvent(type="reasoning", reasoning_delta=rc))
                    c = delta.get("content")
                    if c is None:
                        # strict OpenAI models that refuse return refusal, not content
                        c = delta.get("refusal")
                    if c:
                        content_parts.append(c)
                        # Raw text always accumulates for extraction; only the
                        # UI-facing emission passes through the Hermes gate so
                        # literal <tool_call> XML never leaks to live rendering.
                        visible = hermes_gate.feed(c)
                        if visible:
                            cb(StreamEvent(type="text", delta_text=visible))
                    for tc in delta.get("tool_calls") or []:
                        raw_idx = tc.get("index")
                        fn = tc.get("function") or {}
                        fname = fn.get("name") or ""
                        if isinstance(raw_idx, int):
                            idx = raw_idx
                        else:
                            # Missing "index" collapses parallel calls into
                            # bucket 0. Best-effort repair: a named fragment
                            # unrelated to the current bucket's accumulated
                            # name (neither is a prefix of the other, i.e. not
                            # a streamed continuation) opens a new bucket;
                            # anonymous argument fragments continue the most
                            # recently named bucket, matching the sequential
                            # name-then-arguments shape backends emit. Fully
                            # interleaved index-free streams stay ambiguous.
                            head = partials.get(current_bucket)
                            if (
                                fname
                                and head is not None
                                and head.name
                                and not fname.startswith(head.name)
                                and not head.name.startswith(fname)
                            ):
                                current_bucket = len(partials)
                            idx = current_bucket
                        p = partials.setdefault(idx, _PartialCall())
                        if tc.get("id"):
                            p.id = tc["id"]
                        if fname:
                            p.name += fname
                        if fn.get("arguments"):
                            p.args_buf += fn["arguments"]
                            cb(StreamEvent(type="tool_args_delta"))
                return True

            alive = True
            for raw_line in resp:
                sline = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if sline.startswith("data:"):
                    # SSE frames may spread one logical payload over several
                    # "data:" lines; spec says join them with \n before parsing.
                    data_lines.append(sline[len("data:"):].strip())
                elif not sline and data_lines:
                    # Blank line terminates the frame per the SSE spec.
                    alive = dispatch_frame("\n".join(data_lines))
                    data_lines.clear()
                    if not alive:
                        break
                # event:/id:/retry/comment lines carry nothing we consume.
            if alive and data_lines:
                # Server ended mid-frame without a trailing blank line; the
                # pending payload still deserves a parse attempt.
                dispatch_frame("\n".join(data_lines))

        # Release anything the gate held back that proved to be plain text
        # (e.g. a lone '<' at end of stream); suppressed XML stays hidden —
        # extraction below recovers the calls from the full content instead,
        # mirroring the non-stream path.
        tail = hermes_gate.finish()
        if tail:
            cb(StreamEvent(type="text", delta_text=tail))
        self.total_usage.add(usage)
        content = "".join(content_parts)
        reasoning = "".join(reasoning_parts) or None
        tool_calls: list[ToolCall] = []
        for idx in sorted(partials):
            p = partials[idx]
            try:
                args = json.loads(p.args_buf) if p.args_buf.strip() else {}
            except json.JSONDecodeError:
                args = {"_raw": p.args_buf}
            tool_calls.append(ToolCall(id=p.id or f"call_{idx}", name=p.name, arguments=args))

        msg = Message(role="assistant", content=content or None, reasoning=reasoning, usage=usage)
        if tool_calls:
            msg.tool_calls = tool_calls
        elif isinstance(content, str):
            hermes = parse_hermes_tool_calls(content)
            if hermes:
                msg.tool_calls = hermes
                msg.content = _HERMES_TOOL_RE.sub("", content).strip() or None
        return ModelResponse(message=msg, finish_reason=finish_reason)
