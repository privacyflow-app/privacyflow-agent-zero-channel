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
    "observation_window": 10,
    "min_interval": 15,
    "poll_interval": 3,
    "fallback_schedule": [
        (300, 45, "fallback_short"),      # 0-5 min: every 45s
        (900, 120, "fallback_medium"),     # 5-15 min: every 2 min
        (3600, 300, "fallback_long"),       # 15-60 min: every 5 min
        (10800, 600, "fallback_hours"),     # 1-3 hr: every 10 min
        (999999, 900, "fallback_deep"),     # 3hr+: every 15 min
    ],
    "messages": {
        "initial": "On it, looking into this for you...",
        "subagent": "Digging a bit deeper into this one...",
        "tool_activity": "Still looking into this, got a few things to check...",
        "reviewing": "Found a few leads, just verifying some things...",
        "synthesizing": "Getting close \u2014 putting this together now...",
        "rate_limited": "Still here, just thinking this through...",
        "suppress_ack": "Got it, I'll hold off on the pings. Just say \"update me\" when you want them back.",
        "resume_ack": "Back on it \u2014 I'll keep you posted.",
        "resume_idle_ack": "Sure \u2014 nothing's running right now, but I'll keep you posted on the next one.",
        "fallback_short": "Still on this, it's a complex one \u2014 appreciate the patience...",
        "fallback_medium": "Still working at this, been a few minutes now...",
        "fallback_long": "Still going, about {minutes} minutes in \u2014 this one's a deep dive...",
        "fallback_hours": "Still on this task, about {hours} hour(s) in. It's a big one...",
        "fallback_deep": "This one's been running for about {hours} hours. Still working away at it...",
    },
}

# Max message length for meta-command detection. Longer messages are always
# dispatched to the agent, never intercepted as meta-commands.
_META_CMD_MAX_LEN = 50

# Phrases that suppress progress updates (case-insensitive, exact phrase
# match against the full message text — NOT substring matching, to avoid
# false positives like "keep this quiet" or "hold off on the analysis").
_SUPPRESS_PHRASES = frozenset({
    "no need to update me",
    "no updates",
    "no more updates",
    "stop updates",
    "stop updating me",
    "stop pinging me",
    "stop pinging",
    "don't update me",
    "dont update me",
    "quiet",
    "silence",
    "hold off on updates",
    "hold off on the pings",
    "hold off on pings",
})

# Phrases that resume progress updates.
_RESUME_PHRASES = frozenset({
    "update me",
    "resume updates",
    "send updates",
    "start updating",
    "start updating me",
    "keep me posted",
    "keep me updated",
    "ping me",
    "give me updates",
    "resume pinging",
})

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


def _cleanup_stale_state_mappings() -> None:
    """Remove state.json entries whose chat directories are missing on disk.

    Called at poller startup to prevent lazy-load failures from creating
    duplicate threads when a chat dir was lost (e.g. save failure, manual
    deletion, or deserialization error on a previous run).
    """
    from helpers.persist_chat import _get_chat_file_path

    with _state_lock:
        state = _load_state()
        chats = state.get("chats", {})
        stale_keys = []
        for mapping_key, ctx_id in chats.items():
            chat_path = _get_chat_file_path(ctx_id)
            if not os.path.isfile(chat_path):
                stale_keys.append(mapping_key)

        if not stale_keys:
            return

        for key in stale_keys:
            chats.pop(key, None)
        _save_state(state)
        preview = ", ".join(stale_keys[:5])
        suffix = "..." if len(stale_keys) > 5 else ""
        PrintStyle.info(
            f"[pf_channel] 🧹 Cleaned {len(stale_keys)} stale state.json mapping(s) "
            f"(chat dirs missing on disk): {preview}{suffix}"
        )


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


def _detect_meta_command(text: str) -> str | None:
    """Detect suppress/resume meta-commands from natural language.

    Returns "suppress", "resume", or None.

    Uses exact phrase matching (not substring) against the full stripped
    message text, with a length guard. This prevents false positives like
    "keep this quiet" or "hold off on the analysis" from being eaten.
    Longer messages (>50 chars) are never intercepted — they go to the agent.
    """
    if len(text) > _META_CMD_MAX_LEN:
        return None
    normalized = text.strip().lower().rstrip(".!?")
    if not normalized:
        return None
    if normalized in _SUPPRESS_PHRASES:
        return "suppress"
    if normalized in _RESUME_PHRASES:
        return "resume"
    return None


async def _handle_meta_command(
    context: AgentContext,
    cmd: str,
    contact_id: str,
    messenger: str,
    group_id: str | None,
) -> bool:
    """Handle a suppress/resume meta-command.

    Returns True if the command was handled (caller should return early),
    False if it should fall through to normal dispatch.
    """
    if cmd == "suppress":
        context.data["pf_no_updates"] = True
        # Cancel the running progress task immediately
        task = context.data.get("pf_progress_task")
        if task and not task.done():
            task.cancel()
        await asyncio.to_thread(
            send_message,
            contact_id,
            _PROGRESS_DEFAULTS["messages"]["suppress_ack"],
            messenger,
            group_id,
        )
        PrintStyle.info(f"[pf_channel] Progress updates suppressed by {messenger}")
        return True

    if cmd == "resume":
        context.data["pf_no_updates"] = False
        await asyncio.to_thread(
            send_message,
            contact_id,
            _PROGRESS_DEFAULTS["messages"]["resume_ack"],
            messenger,
            group_id,
        )
        # Restart progress timer if agent is still running
        if context.is_running():
            _start_progress_timer(context, contact_id, messenger, group_id)
        else:
            # Agent already finished — send idle ack instead
            await asyncio.to_thread(
                send_message,
                contact_id,
                _PROGRESS_DEFAULTS["messages"]["resume_idle_ack"],
                messenger,
                group_id,
            )
        PrintStyle.info(f"[pf_channel] Progress updates resumed by {messenger}")
        return True

    return False


def _get_verbosity() -> str:
    """Read progress verbosity from the A0 plugin config.

    Returns "mute", "normal", or "chatty". Falls back to "normal" if
    unset or if the A0 config API is unavailable.

    This reads from the live plugin config (set via the webui form),
    NOT from default_config.yaml — so the <select> in the config form
    actually takes effect.
    """
    try:
        from helpers import plugins
        config = plugins.get_plugin_config("privacyflow_channel") or {}
        return config.get("progress_verbosity", "normal")
    except Exception:
        return "normal"


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
    response, etc.). Falls back to escalating "still working" nudges during
    silent inference (long LLM calls, rate limits), with the interval growing
    for multi-hour tasks.

    The task is stored on context.data['pf_progress_task'] so _50_pf_reply
    can cancel it when the response is ready.

    Verbosity (from A0 plugin config, set via webui form):
      mute   — no progress messages at all
      normal — key milestones only (subagent, synthesizing, reviewing);
               doubled intervals, skips tool_activity and rate_limited
      chatty — every step (current full behavior)
    """
    # Read verbosity from A0 plugin config (webui form), not default_config.yaml
    verbosity = _get_verbosity()
    if verbosity == "mute":
        return

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

    observation_window = pm_cfg.get("observation_window", defaults["observation_window"])
    min_interval = pm_cfg.get("min_interval", defaults["min_interval"])
    poll_interval = pm_cfg.get("poll_interval", defaults["poll_interval"])
    fallback_schedule = pm_cfg.get(
        "fallback_schedule", defaults["fallback_schedule"]
    )

    # "normal" verbosity halves the message rate: double the throttle and
    # fallback intervals. "chatty" uses the raw values. "mute" already returned.
    if verbosity == "normal":
        min_interval = min_interval * 2
        fallback_schedule = [
            (threshold, interval * 2, key)
            for threshold, interval, key in fallback_schedule
        ]

    # Signals to skip in "normal" mode (only major milestones fire)
    _normal_skip_signals = frozenset({"tool_activity", "rate_limited"})

    msg_cfg = pm_cfg.get("messages", defaults["messages"])
    # Merge user-provided messages over defaults so partial overrides work
    messages = {**defaults["messages"], **(msg_cfg if isinstance(msg_cfg, dict) else {})}

    def _get_msg(key: str, **fmt_kwargs) -> str:
        template = messages.get(key, defaults["messages"].get(key, "..."))
        try:
            return template.format(**fmt_kwargs) if fmt_kwargs else template
        except (KeyError, IndexError):
            return template

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

    def _get_fallback_interval(task_elapsed: float) -> tuple[float, str]:
        """Return (interval_seconds, message_key) for the current elapsed time."""
        for threshold, interval, key in fallback_schedule:
            if task_elapsed < threshold:
                return interval, key
        # Fallback to last entry if schedule doesn't cover it
        if fallback_schedule:
            _, interval, key = fallback_schedule[-1]
            return interval, key
        return 45, "fallback_short"

    async def _progress_loop():
        try:
            import time

            # Track log position to diff new entries each tick
            _, log_len = _read_log_safe(context, 0)
            last_log_index = log_len
            prev_agentno = 0

            # Phase 1: Observation window — watch the agent's log for up to
            # observation_window seconds before deciding whether to send an
            # initial ack. This prevents "On it..." from firing for quick
            # responses (agent produces a "response" entry → skip ack) while
            # still acknowledging complex tasks (tool/subagent entries → send
            # ack immediately, or no entries after window → send ack).
            #
            # Note: entries read during observation are NOT consumed —
            # last_log_index is restored after observation so the main loop
            # can re-read them for event-driven messages.
            obs_base_index = last_log_index
            ack_sent = False
            obs_start = time.monotonic()
            while (
                time.monotonic() - obs_start < observation_window
                and context.is_running()
            ):
                if context.data.get("pf_no_updates"):
                    return
                await asyncio.sleep(poll_interval)
                if not context.is_running():
                    return

                new_entries, current_len = _read_log_safe(context, obs_base_index)
                # Update prev_agentno from observations (sub-agent tracking)
                # but DON'T advance last_log_index — main loop re-reads these
                if new_entries:
                    _, prev_agentno = _classify_log_entries(new_entries, prev_agentno)

                # Check for rate-limit
                progress_text = ""
                try:
                    progress_text = context.log.progress or ""
                except Exception:
                    pass
                is_rate_limited = "rate" in progress_text.lower() or "limit" in progress_text.lower()

                if new_entries:
                    # Classify what the agent is doing
                    signal, _ = _classify_log_entries(new_entries, prev_agentno)
                    if signal == "synthesizing":
                        # Agent is already responding — skip ack, answer is coming
                        ack_sent = True
                        break
                    elif signal is not None:
                        # Agent is working (tool, subagent, etc.) — send ack now
                        await _send(_get_msg("initial"), "initial")
                        ack_sent = True
                        break
                elif is_rate_limited:
                    # Agent is rate-limited — send a rate-limit ack
                    if verbosity != "normal":
                        await _send(_get_msg("rate_limited"), "rate_limited")
                    else:
                        await _send(_get_msg("initial"), "initial")
                    ack_sent = True
                    break

            # Observation window expired with no entries — agent is in long
            # inference. Send the initial ack so the user knows the message
            # was received.
            if not ack_sent and context.is_running():
                if not context.data.get("pf_no_updates"):
                    await _send(_get_msg("initial"), "initial")
                    ack_sent = True

            last_msg_time = time.monotonic()
            task_start = last_msg_time

            while context.is_running():
                await asyncio.sleep(poll_interval)
                if not context.is_running():
                    return

                # Check suppress flag — user asked for no updates
                if context.data.get("pf_no_updates"):
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
                task_elapsed = now - task_start

                # Classify log activity
                signal, prev_agentno = _classify_log_entries(new_entries, prev_agentno)

                # Override signal if rate-limited (high priority but below subagent)
                if is_rate_limited and signal not in ("subagent",):
                    signal = "rate_limited"

                # In "normal" mode, skip minor signals (tool_activity, rate_limited)
                if verbosity == "normal" and signal in _normal_skip_signals:
                    signal = None

                if signal and elapsed >= min_interval:
                    await _send(_get_msg(signal), signal)
                    last_msg_time = now
                elif not signal:
                    # Check if it's time for a fallback nudge (escalating interval)
                    fb_interval, fb_key = _get_fallback_interval(task_elapsed)
                    if elapsed >= fb_interval:
                        # Format time-aware message
                        minutes = int(task_elapsed // 60)
                        hours = round(task_elapsed / 3600, 1)
                        await _send(
                            _get_msg(fb_key, minutes=minutes, hours=hours),
                            fb_key,
                        )
                        last_msg_time = now
        except asyncio.CancelledError:
            pass
        except Exception as e:
            PrintStyle.debug(f"[pf_channel] Progress timer error: {format_error(e)}")

    # Reset suppress flag for new task — each task gets fresh updates
    # unless the user explicitly suppresses again.
    context.data.pop("pf_no_updates", None)

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

        # Meta-command interception: check for suppress/resume phrases when
        # the agent is running. Only short messages are checked (≤50 chars)
        # to avoid eating real questions that happen to contain trigger words.
        if context.is_running():
            cmd = _detect_meta_command(text)
            if cmd:
                handled = await _handle_meta_command(
                    context, cmd, contact_id, messenger, group_id
                )
                if handled:
                    return

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
        configured_app_id = _pf_client.get_app_id()
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

    # Clean stale state.json mappings: remove entries whose chat dirs are missing
    # on disk. This prevents lazy-load failures from creating duplicate threads.
    _cleanup_stale_state_mappings()

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
