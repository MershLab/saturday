from __future__ import annotations

import json
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

TELEGRAM_API = "https://api.telegram.org"
DISCORD_API = "https://discord.com/api/v10"
SLACK_API = "https://slack.com/api"
MAX_MESSAGE = 4000
# inbound guard: an oversized message must not pin a worker thread (and its
# provider spend) indefinitely; truncate with a visible note instead
MAX_PROCESSED_TEXT = 8000
SESSION_IDLE_EVICT_S = 30 * 60  # agents are expensive; drop chats idle > 30 min


def redact_token(text: str, token: str) -> str:
    """Bot tokens ride in URLs or Authorization headers, so an error can
    embed one; never print one."""
    if token and token in text:
        return text.replace(token, "***")
    return text


class TelegramTransport:
    """HTTP boundary kept injectable for offline tests."""

    def __init__(self, token: str, api_base: str = TELEGRAM_API, timeout: float = 35) -> None:
        self.token = token
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.offset = 0

    def _call(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.api_base}/bot{self.token}/{method}"
        if payload:
            data = urllib.parse.urlencode(payload).encode()
            req = urllib.request.Request(url, data=data)
        else:
            req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body

    def get_updates(self) -> list[dict[str, Any]]:
        body = self._call("getUpdates", {"offset": self.offset + 1, "timeout": 30})
        updates = body.get("result") or []
        for u in updates:
            self.offset = max(self.offset, int(u.get("update_id", 0)))
        return updates

    def send_message(self, chat_id: Any, text: str) -> None:
        for chunk_start in range(0, len(text), MAX_MESSAGE):
            chunk = text[chunk_start : chunk_start + MAX_MESSAGE]
            self._call("sendMessage", {"chat_id": chat_id, "text": chunk})


class DiscordTransport:
    """HTTP boundary kept injectable for offline tests.

    Polls each configured channel's REST message history rather than opening
    Discord's real-time Gateway websocket, so this stays stdlib-only like the
    Telegram transport - the cost is up-to-poll-interval latency instead of
    push, the same tradeoff Telegram's own long polling already makes.
    """

    def __init__(
        self,
        token: str,
        channel_ids: list[Any],
        api_base: str = DISCORD_API,
        timeout: float = 15,
    ) -> None:
        self.token = token
        self.channel_ids = list(channel_ids)
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self._after: dict[Any, str] = {}
        self._bot_id: str | None = None

    def _call(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        url = f"{self.api_base}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bot {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = resp.read()
        return json.loads(body.decode("utf-8")) if body else None

    def _bot_user_id(self) -> str | None:
        if self._bot_id is None:
            me = self._call("GET", "/users/@me") or {}
            self._bot_id = me.get("id")
        return self._bot_id

    def get_updates(self) -> list[dict[str, Any]]:
        updates: list[dict[str, Any]] = []
        bot_id: str | None = None
        for cid in self.channel_ids:
            after = self._after.get(cid)
            if after is None:
                # bootstrap: seed the cursor at "now" instead of replaying
                # the channel's whole history as though it just arrived
                latest = self._call("GET", f"/channels/{cid}/messages?limit=1") or []
                self._after[cid] = latest[0]["id"] if latest else "0"
                continue
            messages = self._call("GET", f"/channels/{cid}/messages?limit=50&after={after}") or []
            if messages and bot_id is None:
                bot_id = self._bot_user_id()
            for m in sorted(messages, key=lambda m: int(m["id"])):
                self._after[cid] = m["id"]
                author = m.get("author") or {}
                if author.get("bot") or author.get("id") == bot_id:
                    continue
                text = (m.get("content") or "").strip()
                if not text:
                    continue
                updates.append({"chat_id": cid, "text": text})
        return updates

    def send_message(self, chat_id: Any, text: str) -> None:
        for chunk_start in range(0, len(text), MAX_MESSAGE):
            chunk = text[chunk_start : chunk_start + MAX_MESSAGE]
            self._call("POST", f"/channels/{chat_id}/messages", {"content": chunk})


class SlackTransport:
    """HTTP boundary kept injectable for offline tests.

    Polls `conversations.history` per configured channel with the Web API
    rather than Socket Mode or an Events API webhook, so a self-hoster needs
    nothing but a bot token and no public HTTPS endpoint to receive on.
    """

    def __init__(
        self,
        token: str,
        channel_ids: list[Any],
        api_base: str = SLACK_API,
        timeout: float = 15,
    ) -> None:
        self.token = token
        self.channel_ids = list(channel_ids)
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self._oldest: dict[Any, str] = {}
        self._bot_id: str | None = None

    def _call(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.api_base}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json; charset=utf-8")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if not body.get("ok", False):
            raise RuntimeError(f"slack api error: {body.get('error', 'unknown')}")
        return body

    def _bot_user_id(self) -> str | None:
        if self._bot_id is None:
            self._bot_id = self._call("GET", "/auth.test").get("user_id")
        return self._bot_id

    def get_updates(self) -> list[dict[str, Any]]:
        updates: list[dict[str, Any]] = []
        bot_id: str | None = None
        for cid in self.channel_ids:
            oldest = self._oldest.get(cid)
            if oldest is None:
                # bootstrap: start listening from now, not from channel history
                self._oldest[cid] = f"{time.time():.6f}"
                continue
            body = self._call(
                "GET",
                "/conversations.history",
                query={"channel": cid, "oldest": oldest, "inclusive": "false", "limit": 50},
            )
            messages = body.get("messages") or []
            if messages and bot_id is None:
                bot_id = self._bot_user_id()
            for m in sorted(messages, key=lambda m: float(m["ts"])):
                self._oldest[cid] = m["ts"]
                if m.get("bot_id") or m.get("user") == bot_id or m.get("subtype"):
                    continue
                text = (m.get("text") or "").strip()
                if not text:
                    continue
                updates.append({"chat_id": cid, "text": text})
        return updates

    def send_message(self, chat_id: Any, text: str) -> None:
        for chunk_start in range(0, len(text), MAX_MESSAGE):
            chunk = text[chunk_start : chunk_start + MAX_MESSAGE]
            self._call("POST", "/chat.postMessage", {"channel": chat_id, "text": chunk})


@dataclass
class ChatSession:
    agent_factory: Callable[[], Any]
    agent: Any = None
    last_active: float = field(default_factory=time.time)  # doubles as last_used


class _PollingGateway:
    """Shared long-poll dispatch loop: session lifecycle, per-chat locking,
    idle eviction, backoff, run_forever. A subclass owns only how its
    platform's raw update maps to (chat_id, text) via `_extract`, and which
    transport it defaults to."""

    def __init__(
        self,
        transport: Any,
        agent_factory: Callable[[], Any],
        allowed_chat_ids: set[Any] | None = None,
    ) -> None:
        self.transport = transport
        self.agent_factory = agent_factory
        self.allowed = allowed_chat_ids
        self.sessions: dict[Any, ChatSession] = {}
        self._sessions_lock = threading.Lock()  # guards sessions + _chat_locks
        self._chat_locks: dict[Any, threading.Lock] = {}
        self._unauth_warned: set[Any] = set()  # one liveness reply per stranger chat
        self.running = False
        self.consecutive_failures = 0

    def _extract(self, update: dict[str, Any]) -> tuple[Any, str] | None:
        """Return (chat_id, text), or None if the update carries no message.
        Default assumes an already-normalized {"chat_id", "text"} update, as
        the Discord/Slack transports produce; Telegram overrides this."""
        chat_id = update.get("chat_id")
        text = (update.get("text") or "").strip()
        if chat_id is None or not text:
            return None
        return chat_id, text

    def _lock_for(self, chat_id: Any) -> threading.Lock:
        """One-at-a-time processing PER CHAT (ordering within a conversation);
        different chats dispatch concurrently so one slow run cannot stall the
        rest of the gateway."""
        with self._sessions_lock:
            lock = self._chat_locks.get(chat_id)
            if lock is None:
                lock = threading.Lock()
                self._chat_locks[chat_id] = lock
            return lock

    def _evict_idle_sessions(self) -> None:
        cutoff = time.time() - SESSION_IDLE_EVICT_S
        with self._sessions_lock:
            stale = [cid for cid, s in self.sessions.items() if s.last_active < cutoff]
            for cid in stale:
                del self.sessions[cid]
                self._chat_locks.pop(cid, None)

    def session_for(self, chat_id: Any):
        self._evict_idle_sessions()
        with self._sessions_lock:
            sess = self.sessions.get(chat_id)
            if sess is None or time.time() - sess.last_active > 3600:
                sess = ChatSession(agent_factory=self.agent_factory, agent=self.agent_factory())
                self.sessions[chat_id] = sess
            sess.last_active = time.time()
            return sess

    def handle_update(self, update: dict[str, Any]) -> bool:
        """Parse + authorize an update and hand it to a worker thread.

        Returns True when the update will be processed. Never runs the agent
        inline: the single poll loop must keep polling while chats work.
        """
        parsed = self._extract(update)
        if parsed is None:
            return False
        chat_id, text = parsed
        if self.allowed is not None and chat_id not in self.allowed and str(chat_id) not in {str(c) for c in self.allowed}:
            # liveness discipline: replying to every unauthorized message turns
            # the bot into a free oracle that confirms its own existence (and
            # pays outbound traffic for any stranger who finds the bot name).
            # Warn ONCE per chat so a misconfigured allowlist is discoverable;
            # after that, silent drop.
            with self._sessions_lock:
                first_time = chat_id not in self._unauth_warned
                if first_time:
                    self._unauth_warned.add(chat_id)
            if first_time:
                try:
                    self.transport.send_message(chat_id, "Not authorized for this bot.")
                except Exception:
                    pass  # never let a probe's reply failure reach the poll loop
            return False
        if len(text) > MAX_PROCESSED_TEXT:
            text = text[:MAX_PROCESSED_TEXT] + f"\n[truncated: message exceeded {MAX_PROCESSED_TEXT} characters]"
        # resolve the session on the poll thread: workers then never race the
        # idle-eviction/recreate logic in session_for()
        sess = self.session_for(chat_id)
        threading.Thread(
            target=self._process,
            args=(chat_id, sess, text),
            daemon=True,
            name=f"saturday-gw-chat-{chat_id}",
        ).start()
        return True

    def _process(self, chat_id: Any, sess: ChatSession, text: str) -> None:
        from saturday.sessions import RunState

        lock = self._lock_for(chat_id)
        with lock:
            run_state: RunState | None = None

            def on_session_id(sid: str) -> None:
                nonlocal run_state
                from saturday.config import get_config_dir

                run_state = RunState(get_config_dir() / "sessions", sid)
                run_state.start()

            def on_step_start(_step_index: int) -> None:
                # only blocks this chat's own dispatch thread - other chats
                # each get their own thread and lock, so a paused chat
                # doesn't stall anyone else's.
                if run_state is None:
                    return
                while run_state.pause_requested():
                    time.sleep(1.0)

            try:
                traj = sess.agent.run(
                    text,
                    on_session_id=on_session_id,
                    on_tool_result=lambda _r: run_state.heartbeat() if run_state else None,
                    on_step_start=on_step_start,
                )
                reply = traj.final_answer or f"[stopped: {traj.stop_reason}]"
                if run_state is not None:
                    run_state.done()
            except Exception as exc:
                reply = f"agent error: {type(exc).__name__}: {exc}"
                if run_state is not None:
                    run_state.mark_crashed()
            try:
                self.transport.send_message(chat_id, reply)
            except Exception as exc:
                detail = redact_token(f"{type(exc).__name__}: {exc}", getattr(self.transport, "token", ""))
                print(f"[gateway] send failed for chat {chat_id}: {detail}", file=sys.stderr)
            finally:
                sess.last_active = time.time()

    def poll_once(self) -> int:
        handled = 0
        try:
            updates = self.transport.get_updates()
        except Exception as exc:
            detail = redact_token(f"{type(exc).__name__}: {exc}", getattr(self.transport, "token", ""))
            print(f"[gateway] poll failed ({detail})", file=sys.stderr)
            return 0
        for u in updates:
            if self.handle_update(u):
                handled += 1
        return handled

    def _tick(self, sleep: Callable[[float], None]) -> bool:
        """One poll cycle. Returns False after recording a failure backoff sleep."""
        try:
            updates = self.transport.get_updates()
        except Exception as exc:
            self.consecutive_failures += 1
            delay = min(2 ** min(self.consecutive_failures, 5), 30)
            detail = redact_token(f"{type(exc).__name__}: {exc}", getattr(self.transport, "token", ""))
            print(
                f"[gateway] poll failed ({detail}); retrying in {delay}s",
                file=sys.stderr,
            )
            sleep(delay)
            return False
        self.consecutive_failures = 0
        for u in updates:
            try:
                self.handle_update(u)
            except Exception as exc:
                # a bad update must be visible, not silently swallowed mid-loop
                detail = redact_token(f"{type(exc).__name__}: {exc}", getattr(self.transport, "token", ""))
                print(f"[gateway] update dispatch failed: {detail}", file=sys.stderr)
        return True

    def run_forever(self, sleep_s: float = 1.5, sleep: Callable[[float], None] | None = None) -> None:
        import time as _time

        sleeper = sleep or _time.sleep
        self.running = True
        while self.running:
            self._tick(sleeper)
            if self.running and not self.consecutive_failures:
                sleeper(sleep_s)

    def stop(self) -> None:
        self.running = False


class TelegramGateway(_PollingGateway):
    def __init__(
        self,
        token: str,
        agent_factory: Callable[[], Any],
        allowed_chat_ids: set[int | str] | None = None,
        transport: TelegramTransport | None = None,
    ) -> None:
        super().__init__(transport or TelegramTransport(token), agent_factory, allowed_chat_ids)

    def _extract(self, update: dict[str, Any]) -> tuple[Any, str] | None:
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return None
        chat_id = (msg.get("chat") or {}).get("id")
        text = (msg.get("text") or "").strip()
        if chat_id is None or not text:
            return None
        return chat_id, text


class DiscordGateway(_PollingGateway):
    """Same dispatch loop as TelegramGateway; unlike Telegram there is no
    "allow-all" mode; a bot only ever polls the channel ids it's given."""

    def __init__(
        self,
        token: str,
        agent_factory: Callable[[], Any],
        channel_ids: list[Any],
        transport: DiscordTransport | None = None,
    ) -> None:
        super().__init__(transport or DiscordTransport(token, channel_ids), agent_factory, set(channel_ids))


class SlackGateway(_PollingGateway):
    """Same dispatch loop as TelegramGateway; unlike Telegram there is no
    "allow-all" mode; a bot only ever polls the channel ids it's given."""

    def __init__(
        self,
        token: str,
        agent_factory: Callable[[], Any],
        channel_ids: list[Any],
        transport: SlackTransport | None = None,
    ) -> None:
        super().__init__(transport or SlackTransport(token, channel_ids), agent_factory, set(channel_ids))


def build_gateway_agent(cfg_overrides: dict | None = None):
    from saturday.agent.core import Agent
    from saturday.config import AgentConfig

    cfg = AgentConfig.load(cfg_overrides or {})

    def factory():
        return Agent(cfg=cfg)

    return factory
