"""
PrivacyFlow Channel Auto-Reply Extension

Fires when the agent finishes processing.
If the context has PF routing metadata, extracts the agent's response
and sends it back to PrivacyFlow via the send API.

Graceful steering: if pf_steer flag is set (new message arrived while
agent was busy), the response is discarded and the stored message is
dispatched instead — no kill, no interrupt.
"""

import asyncio
import importlib.util
import os
from typing import Any

from helpers.extension import Extension
from helpers.print_style import PrintStyle
from helpers.errors import format_error
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
_message_splitter = _load_helper("message_splitter")
send_message = _pf_client.send_message
split_message = _message_splitter.split_message


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


class PfAutoReply(Extension):
    """Send agent response back to PrivacyFlow."""

    async def execute(self, **kwargs: Any) -> None:
        if not self.agent or self.agent.number != 0:
            return

        context = self.agent.context
        pf_routing = context.data.get("pf_routing")
        if not pf_routing:
            return

        # Graceful steering: discard response and dispatch stored message
        if context.data.get("pf_steer"):
            steer_msg = context.data.pop("pf_steer_msg", None)
            context.data.pop("pf_steer", None)

            PrintStyle.info(
                f"[pf_reply] 🔄 Steered: discarding response, "
                f"dispatching new message"
            )
            context.log.log(
                type="info",
                content="🔄 Steered: previous response discarded for new message.",
            )

            if steer_msg:
                # Dispatch the stored message
                context.communicate(
                    UserMessage(message=steer_msg["text"], id=steer_msg["msg_id"])
                )
                save_tmp_chat(context)
            return

        # Normal flow: extract and send response
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
