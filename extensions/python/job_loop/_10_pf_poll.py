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


def _format_message_text(msg: dict) -> str:
    """Format message text. Prefix group messages with [contactId]."""
    if msg.get("isGroupMessage") and msg.get("groupId"):
        return f"[{msg['contactId']}]: {msg['content']}"
    return msg.get("content", "")


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
            # Context was garbage collected, remove stale mapping
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


async def _poll_loop() -> None:
    """Background poll loop. Runs until cancelled."""
    PrintStyle.info(f"[pf_channel] 🔄 Poller started (interval: {POLL_INTERVAL_SEC}s)")

    while True:
        try:
            response = await asyncio.to_thread(poll_messages, 10)
            messages = response.get("messages", [])

            for msg in messages:
                # Skip command messages
                if msg.get("isCommand"):
                    continue

                contact_id = msg.get("contactId", "")
                group_id = msg.get("groupId")
                messenger = msg.get("messenger", "")

                if not contact_id or not messenger:
                    continue

                # Get or create context (persisted across polls)
                context = _get_or_create_context(msg)

                # Update routing metadata (in case context was reused)
                mapping_key = group_id if group_id else contact_id
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

                # Dispatch to agent
                context.communicate(UserMessage(message=text, id=msg_id))

                # Persist chat so it survives restarts
                save_tmp_chat(context)

                PrintStyle.info(
                    f"[pf_channel] ✅ Forwarded message from {messenger} "
                    f"→ context {context.id} ({'group' if group_id else 'DM'})"
                )

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
