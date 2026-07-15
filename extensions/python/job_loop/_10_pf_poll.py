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


POLL_INTERVAL_SEC = 3
_poll_task: asyncio.Task | None = None

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
