"""
PrivacyFlow Channel Poller

Runs as a background asyncio task started from job_loop.
Polls PrivacyFlow for incoming messages every few seconds,
forwards them to the appropriate Agent Zero context.
"""

import asyncio
import importlib.util
import os
from typing import Any

from helpers.extension import Extension
from helpers.print_style import PrintStyle
from helpers.errors import format_error
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


def _format_message_text(msg: dict) -> str:
    """Format message text. Prefix group messages with [contactId]."""
    if msg.get("isGroupMessage") and msg.get("groupId"):
        return f"[{msg['contactId']}]: {msg['content']}"
    return msg.get("content", "")


def _find_context_by_mapping_key(mapping_key: str) -> AgentContext | None:
    """Find existing context by PF routing mapping key."""
    for ctx in AgentContext.all():
        pf_routing = ctx.data.get("pf_routing")
        if pf_routing and pf_routing.get("mapping_key") == mapping_key:
            return ctx
    return None


def _get_or_create_context(msg: dict) -> AgentContext:
    """Get existing context or create new one for this contact/group."""
    contact_id = msg.get("contactId", "")
    group_id = msg.get("groupId")
    mapping_key = group_id if group_id else contact_id

    context = _find_context_by_mapping_key(mapping_key)
    if context:
        return context

    from initialize import initialize_agent
    context = AgentContext(
        config=initialize_agent(),
        set_current=False,
    )
    PrintStyle.info(f"[pf_channel] Created new context {context.id} for {mapping_key}")
    return context


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

                # Get or create context
                context = _get_or_create_context(msg)

                # Store routing metadata
                mapping_key = group_id if group_id else contact_id
                context.data["pf_routing"] = {
                    "contact_id": contact_id,
                    "group_id": group_id,
                    "messenger": messenger,
                    "mapping_key": mapping_key,
                }

                # Send message to agent
                text = _format_message_text(msg)
                user_msg = UserMessage(message=text, id=msg.get("messageId", ""))
                context.communicate(user_msg)

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
