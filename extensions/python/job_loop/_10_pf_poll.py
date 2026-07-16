"""
PrivacyFlow Channel Poller

Runs as a background asyncio task started from job_loop.
Polls PrivacyFlow for incoming messages every few seconds,
forwards them to the appropriate Agent Zero context.

Key design (mirrors _telegram_integration patterns):
- Persists context mappings to a JSON state file so the same chat is reused
  across polls and A0 restarts (no new chat per message).
- Calls mq.log_user_message() before context.communicate() so the incoming
  message text is visible in the A0 UI.
- Calls save_tmp_chat() after dispatching so the chat persists.
- Gives each context a human-readable name like 'PF: signal <contactId>'.
- Graceful steering with queue: if agent is busy, messages are queued.
  When agent finishes, the stale response is discarded and the most recent
  queued message is dispatched. Older queued messages are already visible
  in the UI via mq.log_user_message().
- Per-context asyncio lock prevents race conditions where multiple
  dispatch tasks see is_running() as False simultaneously.
"""

import asyncio
import importlib.util
import json
import os
import threading
import uuid
from typing import Any

from helpers.extension import Extension
from helpers.print_style import PrintStyle
from helpers.errors import format_error
from helpers import files
from helpers import message_queue as mq
from helpers.persist_chat import save_tmp_chat
from agent import AgentContext, UserMessage


# Load helpers via importlib since user plugins can't use `from plugins.*` imports
_PLUGIN_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def _load_helper(name: str):
    path = os.path.join(_PLUGIN_DIR, "helpers", f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"privacyflow_channel.helpers.{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_pf_client = _load_helper("pf_client")
poll_messages = _pf_client.poll_messages
is_configured = _pf_client.is_configured
send_message = _pf_client.send_message


POLL_INTERVAL_SEC = 3
_poll_task: asyncio.Task | None = None

# Default progress-message config. `default_config.yaml` is the source of
# truth when present; these constants serve as the documented fallback and
# keep the default message list in one place (no duplicated literals).
_PROGRESS_DEFAULTS = {
    "enabled": True,
    "initial_delay": 5,
    "min_interval": 15,
    "fallback_interval": 45,
    "poll_interval": 3,
    "messages": {
        "initial": "On it, looking into this for you...",
        "subagent": "Digging a bit deeper into this one...",
        "tool_activity": "Still looking into this, got a few things to check...",
        "reviewing": "Found a few leads, just verifying some things...",
        "synthesizing": "Getting close \u2014 putting this together now...",
        "rate_limited": "Still here, just thinking this through...",
        "fallback": "Still on this, it's a complex one \u2014 appreciate the patience...",
        "fallback_alt": "Still working away at this, haven't forgotten about you...",
    },
}

# Per-context dispatch locks: mapping_key -> asyncio.Lock
_dispatch_locks: dict[str, asyncio.Lock] = {}

# State persistence: mapping_key -> context_id
_STATE_FILE = "usr/plugins/privacyflow_channel/state.json"
_state_lock = threading.Lock()


def _load_state() -> dict:
    """Load persisted state (context mappings)."""
    path = files.get_abs_path(_STATE_FILE)
    if os.path.isfile(path):
        try:
            return json.loads(files.read_file(path))
        except Exception:
            return {}
    return {}


def _save_state(state: dict):
    """Persist state (context mappings)."""
    path = files.get_abs_path(_STATE_FILE)
    files.make_dirs(path)
    files.write_file(path, json.dumps(state))


def _try_load_chat_from_disk(ctx_id: str) -> AgentContext | None:
    """Try to lazy-load a specific chat from disk.

    Called when AgentContext.get(ctx_id) returns None but state.json has
    a mapping for this context. This handles the case where load_tmp_chats()
    at startup didn't restore the context (e.g. deserialization error, GC,
    or timing issue).
    """
    try:
        from helpers.persist_chat import _get_chat_file_path, _deserialize_context
        from helpers import files as _files

        path = _get_chat_file_path(ctx_id)
        if not os.path.isfile(path):
            return None

        js = _files.read_file(path)
        data = json.loads(js)
        ctx = _deserialize_context(data)
        PrintStyle.info(f"[pf_channel] 📂 Lazy-loaded context {ctx.id} from disk")
        return ctx
    except Exception as e:
        PrintStyle.error(f"[pf_channel] Failed to lazy-load context {ctx_id}: {format_error(e)}")
        return None


def _classify_log_entries(entries: list, prev_agentno: int) -> tuple[str | None, int]:
    """Classify new log entries into a progress-signal category.

    Returns (signal, new_agentno) where signal is one of the keys in
    _PROGRESS_DEFAULTS["messages"] or None if no notable signal was found,
    and new_agentno is the latest agent number seen (for sub-agent tracking).

    Priority (highest first):
      subagent > synthesizing > rate_limited > tool_activity > reviewing
    """
    signal = None
    new_agentno = prev_agentno
    tool_count = 0
    has_response = False

    for entry in entries:
        etype = getattr(entry, "type", None)
        eagentno = getattr(entry, "agentno", 0)

        if eagentno > new_agentno:
            new_agentno = eagentno

        if etype == "subagent":
            signal = "subagent"
        elif etype == "response":
            has_response = True
        elif etype in ("tool", "code_exe", "browser", "mcp"):
            tool_count += 1
        elif etype == "error":
            # Don't surface errors directly — treat as activity
            tool_count += 1

    # Determine signal by priority if not already set to subagent
    if signal is None:
        if has_response:
            signal = "synthesizing"
        elif new_agentno > prev_agentno:
            signal = "subagent"
        elif tool_count >= 3:
            signal = "reviewing"
        elif tool_count >= 1:
            signal = "tool_activity"

    return signal, new_agentno


def _read_log_safe(context: AgentContext, start_index: int) -> tuple[list, int]:
    """Thread-safe read of log entries from start_index onwards.

    Returns (new_entries, current_length). Uses context.log._lock for
    thread safety, matching the pattern in _get_logs_safe().
    """
    log = context.log
    try:
        with log._lock:
            logs = list(log.logs)
            return logs[start_index:], len(logs)
    except Exception:
        return [], start_index


def _start_progress_timer(
    context: AgentContext,
    contact_id: str,
    messenger: str,
    group_id: str | None,
) -> None:
    """Start an async task that sends human-like progress messages.

    Instead of a fixed-interval timer with generic messages, this watches
    context.log for new entries and generates messages based on what the
    agent is actually doing (sub-agent spawned, tool calls, synthesizing
    response, etc.). Falls back to "still working" nudges during silent
    inference (long LLM calls, rate limits).

    The task is stored on context.data['pf_progress_task'] so _50_pf_reply
    can cancel it when the response is ready.
    """
    import yaml

    # Load config
    config_path = os.path.join(_PLUGIN_DIR, "default_config.yaml")
    try:
        with open(config_path, "r") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        cfg = {}

    pm_cfg = cfg.get("progress_messages", {})
    defaults = _PROGRESS_DEFAULTS
    if not pm_cfg.get("enabled", defaults["enabled"]):
        return

    initial_delay = pm_cfg.get("initial_delay", defaults["initial_delay"])
    min_interval = pm_cfg.get("min_interval", defaults["min_interval"])
    fallback_interval = pm_cfg.get("fallback_interval", defaults["fallback_interval"])
    poll_interval = pm_cfg.get("poll_interval", defaults["poll_interval"])
    msg_cfg = pm_cfg.get("messages", defaults["messages"])
    # Merge user-provided messages over defaults so partial overrides work
    messages = {**defaults["messages"], **(msg_cfg if isinstance(msg_cfg, dict) else {})}

    def _get_msg(key: str) -> str:
        return messages.get(key, defaults["messages"].get(key, "..."))

    async def _send(msg_text: str, label: str) -> None:
        try:
            await asyncio.to_thread(
                send_message,
                contact_id,
                msg_text,
                messenger,
                group_id,
            )
            PrintStyle.info(
                f"[pf_channel] \U0001f4e1 Progress [{label}] sent to {messenger}"
            )
        except Exception as e:
            PrintStyle.debug(
                f"[pf_channel] Progress message send failed: {format_error(e)}"
            )

    async def _progress_loop():
        try:
            # Track log position to diff new entries each tick
            _, log_len = _read_log_safe(context, 0)
            last_log_index = log_len
            prev_agentno = 0
            fallback_count = 0

            # Initial acknowledgement after delay
            await asyncio.sleep(initial_delay)
            if not context.is_running():
                return
            await _send(_get_msg("initial"), "initial")
            import time
            last_msg_time = time.monotonic()

            while context.is_running():
                await asyncio.sleep(poll_interval)
                if not context.is_running():
                    return

                # Read new log entries since last check
                new_entries, current_len = _read_log_safe(context, last_log_index)
                last_log_index = current_len

                # Check for rate-limit in log.progress
                progress_text = ""
                try:
                    progress_text = context.log.progress or ""
                except Exception:
                    pass
                is_rate_limited = "rate" in progress_text.lower() or "limit" in progress_text.lower()

                now = time.monotonic()
                elapsed = now - last_msg_time

                # Classify log activity
                signal, prev_agentno = _classify_log_entries(new_entries, prev_agentno)

                # Override signal if rate-limited (high priority but below subagent)
                if is_rate_limited and signal not in ("subagent",):
                    signal = "rate_limited"

                if signal and elapsed >= min_interval:
                    await _send(_get_msg(signal), signal)
                    last_msg_time = now
                    fallback_count = 0
                elif not signal and elapsed >= fallback_interval:
                    # No notable events — send a fallback nudge
                    fb_key = "fallback" if fallback_count % 2 == 0 else "fallback_alt"
                    await _send(_get_msg(fb_key), fb_key)
                    last_msg_time = now
                    fallback_count += 1
        except asyncio.CancelledError:
            pass
        except Exception as e:
            PrintStyle.debug(f"[pf_channel] Progress timer error: {format_error(e)}")

    # Cancel any existing progress timer
    existing = context.data.get("pf_progress_task")
    if existing and not existing.done():
        existing.cancel()

    context.data["pf_progress_task"] = asyncio.create_task(_progress_loop())


def _format_message_text(msg: dict) -> str:
    """Format message text. Prefix group messages with [contactId]."""
    if msg.get("isGroupMessage") and msg.get("groupId"):
        return f"[{msg['contactId']}]: {msg['content']}"
    return msg.get("content", "")


def _get_dispatch_lock(mapping_key: str) -> asyncio.Lock:
    """Get or create an asyncio lock for a mapping_key.

    This prevents race conditions where multiple dispatch tasks for the same
    context check is_running() simultaneously before the first one calls
    communicate().
    """
    if mapping_key not in _dispatch_locks:
        _dispatch_locks[mapping_key] = asyncio.Lock()
    return _dispatch_locks[mapping_key]


def _get_or_create_context(msg: dict) -> AgentContext:
    """Get existing context or create new one for this contact/group.

    Uses a JSON state file to persist mapping_key -> context_id so the same
    chat is reused across polls and A0 restarts.
    """
    contact_id = msg.get("contactId", "")
    group_id = msg.get("groupId")
    messenger = msg.get("messenger", "")
    mapping_key = group_id if group_id else contact_id

    with _state_lock:
        state = _load_state()
        chats = state.setdefault("chats", {})
        ctx_id = chats.get(mapping_key)

        # Check if existing context is still alive
        if ctx_id:
            ctx = AgentContext.get(ctx_id)
            if ctx:
                return ctx

            # Context not in memory — try lazy-loading from disk
            ctx = _try_load_chat_from_disk(ctx_id)
            if ctx:
                PrintStyle.info(f"[pf_channel] 📂 Lazy-loaded context {ctx_id} from disk")
                return ctx

            # Context truly gone, remove stale mapping
            chats.pop(mapping_key, None)

        # Create new context
        from initialize import initialize_agent
        display_name = f"{messenger} {contact_id[:8]}" if contact_id else mapping_key[:12]
        ctx = AgentContext(
            config=initialize_agent(),
            name=f"PF: {display_name}",
            set_current=False,
        )

        # Store routing metadata on context
        ctx.data["pf_routing"] = {
            "contact_id": contact_id,
            "group_id": group_id,
            "messenger": messenger,
            "mapping_key": mapping_key,
        }

        chats[mapping_key] = ctx.id
        _save_state(state)

        PrintStyle.success(
            f"[pf_channel] New chat {ctx.id} for {display_name} "
            f"({'group' if group_id else 'DM'})"
        )
        return ctx


async def _dispatch_message(msg: dict) -> None:
    """Process a single incoming message.

    Uses a per-context asyncio lock to prevent race conditions where
    multiple dispatch tasks check is_running() simultaneously.

    Graceful steering with queue: if the agent is already processing a
    message on this context, the new message is appended to a queue
    (pf_steer_queue). When the agent finishes, the stale response is
    discarded and the most recent queued message is dispatched.
    Older queued messages are already visible in the UI via
    mq.log_user_message().
    """
    try:
        contact_id = msg.get("contactId", "")
        group_id = msg.get("groupId")
        messenger = msg.get("messenger", "")

        if not contact_id or not messenger:
            return

        mapping_key = group_id if group_id else contact_id

        # Get or create context (persisted across polls)
        context = _get_or_create_context(msg)

        # Update routing metadata (in case context was reused)
        context.data["pf_routing"] = {
            "contact_id": contact_id,
            "group_id": group_id,
            "messenger": messenger,
            "mapping_key": mapping_key,
        }

        # Log incoming message to UI so it's visible in the chat
        text = _format_message_text(msg)
        msg_id = str(uuid.uuid4())
        mq.log_user_message(
            context,
            text,
            [],
            message_id=msg_id,
            source=f" ({messenger})",
        )

        # Use per-context lock to prevent race conditions
        lock = _get_dispatch_lock(mapping_key)
        async with lock:
            # Graceful steering: if agent is busy, queue the message
            if context.is_running():
                queue = context.data.setdefault("pf_steer_queue", [])
                queue.append({
                    "text": text,
                    "msg_id": msg_id,
                    "messenger": messenger,
                })
                PrintStyle.info(
                    f"[pf_channel] 🔄 Queued message from {messenger} "
                    f"(queue depth: {len(queue)})"
                )
                return

            # Agent is idle — dispatch normally
            context.communicate(UserMessage(message=text, id=msg_id))

            # Start progress check-in timer so the user gets feedback
            _start_progress_timer(context, contact_id, messenger, group_id)

            # Persist chat so it survives restarts
            save_tmp_chat(context)

            PrintStyle.info(
                f"[pf_channel] ✅ Forwarded message from {messenger} "
                f"→ context {context.id} ({'group' if group_id else 'DM'})"
            )
    except Exception as e:
        PrintStyle.error(f"[pf_channel] ❌ Dispatch failed: {format_error(e)}")


async def _poll_loop() -> None:
    """Background poll loop. Runs until cancelled.

    Each incoming message is dispatched as a separate asyncio task so the
    poll loop never blocks on agent processing. Messages for the same
    context are serialized by per-context asyncio locks.
    """
    PrintStyle.info(f"[pf_channel] 🔄 Poller started (interval: {POLL_INTERVAL_SEC}s)")

    # Verify auth before starting poll loop — fail fast on bad credentials
    try:
        auth_result = await asyncio.to_thread(_pf_client.verify_auth)
        if not auth_result.get("valid"):
            PrintStyle.error("[pf_channel] ❌ Auth verification failed: API key returned invalid=false")
            return
        app_ids = auth_result.get("appIds", [])
        configured_app_id = _pf_client._get_app_id()
        if configured_app_id and configured_app_id not in app_ids:
            PrintStyle.error(
                f"[pf_channel] ❌ App ID '{configured_app_id}' not authorized. "
                f"Valid app IDs: {', '.join(app_ids) if app_ids else '(none)'}"
            )
            return
        PrintStyle.success(
            f"[pf_channel] ✅ Auth verified — authorized for {len(app_ids)} app(s)"
        )
    except Exception as e:
        PrintStyle.error(f"[pf_channel] ❌ Auth verification failed: {format_error(e)}")
        return

    while True:
        try:
            response = await asyncio.to_thread(poll_messages, 10)
            messages = response.get("messages", [])

            for msg in messages:
                # Dispatch concurrently — don't block the poll loop
                asyncio.create_task(_dispatch_message(msg))

        except Exception as e:
            PrintStyle.error(f"[pf_channel] ❌ Poll failed: {format_error(e)}")

        await asyncio.sleep(POLL_INTERVAL_SEC)


class PfPoller(Extension):
    """Start the PrivacyFlow poller on job_loop tick."""

    async def execute(self, **kwargs: Any) -> None:
        global _poll_task

        # Don't start if not configured
        if not is_configured():
            return

        # Start poll task if not running or dead
        if _poll_task is None or _poll_task.done():
            _poll_task = asyncio.create_task(_poll_loop())
