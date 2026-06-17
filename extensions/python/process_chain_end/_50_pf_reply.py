"""
PrivacyFlow Channel Auto-Reply Extension

Fires when the agent finishes processing.
If the context has PF routing metadata, extracts the agent's response
and sends it back to PrivacyFlow via the send API.
"""

import asyncio
from typing import Any

from helpers.extension import Extension
from helpers.print_style import PrintStyle
from helpers.errors import format_error
from agent import AgentContext

from plugins.privacyflow_channel.helpers.pf_client import send_message
from plugins.privacyflow_channel.helpers.message_splitter import split_message


def _get_logs_safe(context: AgentContext) -> list:
    """Get a thread-safe snapshot of context log entries.

    Prefers a public API if available, falls back to the internal lock.
    TODO: Replace with public thread-safe log accessor when A0 framework provides one.
    """
    log = context.log
    if hasattr(log, "get_logs") and callable(log.get_logs):
        return log.get_logs()
    if hasattr(log, "snapshot") and callable(log.snapshot):
        return log.snapshot()
    with log._lock:
        return list(log.logs)


def _extract_last_response(context: AgentContext) -> str:
    """Extract the last response from context log entries."""
    logs = _get_logs_safe(context)
    if not logs:
        return ""
    for item in reversed(logs):
        if item.type == "response":
            return item.content or ""
    return ""


class PfAutoReply(Extension):
    """Send agent response back to PrivacyFlow."""

    async def execute(self, **kwargs: Any) -> None:
        if not self.agent or self.agent.number != 0:
            return

        context = self.agent.context
        pf_routing = context.data.get("pf_routing")
        if not pf_routing:
            return

        response_text = _extract_last_response(context)
        if not response_text:
            return

        contact_id = pf_routing.get("contact_id", "")
        group_id = pf_routing.get("group_id")
        messenger = pf_routing.get("messenger", "")

        if not contact_id or not messenger:
            PrintStyle.error("[pf_reply] Missing routing metadata")
            return

        # Split and send each chunk
        chunks = split_message(response_text, messenger)
        for chunk in chunks:
            try:
                await asyncio.to_thread(
                    send_message,
                    contact_id,
                    chunk,
                    messenger,
                    group_id,
                )
                PrintStyle.info(
                    f"[pf_reply] ✅ Sent response chunk ({messenger}, {len(chunk)} chars)"
                )
            except Exception as e:
                PrintStyle.error(f"[pf_reply] Failed to send: {format_error(e)}")

        # Clear routing metadata after send
        context.data.pop("pf_routing", None)
