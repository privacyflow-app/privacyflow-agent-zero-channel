"""
PrivacyFlow Channel Auto-Reply Extension

Fires when the agent finishes processing (process_chain_end).
If the context has PF routing metadata, extracts the agent's response
and POSTs it to the bridge service HTTP API for delivery.
"""

import os
import asyncio
import requests

from helpers.extension import Extension
from helpers.print_style import PrintStyle
from helpers.errors import format_error
from agent import AgentContext


def _extract_last_response(context: AgentContext) -> str:
    """Extract the last response from context log entries."""
    with context.log._lock:
        logs = list(context.log.logs)
    if not logs:
        return ""
    for item in reversed(logs):
        if item.type == "response":
            return item.content or ""
    return ""


def _post_to_bridge(bridge_url: str, payload: dict) -> None:
    """Synchronous POST to bridge HTTP API (called via asyncio.to_thread)."""
    response = requests.post(
        f"{bridge_url}/api/response",
        json=payload,
        timeout=30,
    )
    if response.status_code != 200:
        raise Exception(f"Bridge returned {response.status_code}: {response.text}")


class PfAutoReply(Extension):
    """Send agent response back to PrivacyFlow via bridge HTTP API."""

    async def execute(self, **kwargs):
        if not self.agent or self.agent.number != 0:
            return

        context = self.agent.context
        pf_routing = context.data.get("pf_routing")
        if not pf_routing:
            return

        # Extract the agent's response
        response_text = _extract_last_response(context)
        if not response_text:
            return

        contact_id = pf_routing.get("contact_id", "")
        group_id = pf_routing.get("group_id")
        messenger = pf_routing.get("messenger", "")

        if not contact_id or not messenger:
            PrintStyle.error("[pf_reply] Missing routing metadata")
            return

        # Get bridge HTTP API URL
        bridge_url = os.getenv("PF_BRIDGE_URL", "")
        if not bridge_url:
            PrintStyle.error("[pf_reply] PF_BRIDGE_URL not configured")
            return

        bridge_url = bridge_url.rstrip("/")

        # Split message if needed
        from plugins._privacyflow_channel.helpers.message_splitter import split_message
        chunks = split_message(response_text, messenger)

        # Send each chunk to bridge HTTP API
        for chunk in chunks:
            payload = {
                "contact_id": contact_id,
                "messenger": messenger,
                "message": chunk,
            }
            if group_id:
                payload["group_id"] = group_id

            try:
                await asyncio.to_thread(_post_to_bridge, bridge_url, payload)
                PrintStyle.info(
                    f"[pf_reply] ✅ Sent response chunk to bridge "
                    f"({messenger}, {len(chunk)} chars)"
                )
            except Exception as e:
                PrintStyle.error(f"[pf_reply] Failed to send response: {format_error(e)}")

        # Clear routing metadata after send
        context.data.pop("pf_routing", None)
