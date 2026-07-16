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
    PrintStyle.debug("[pf_reply] Falling back to context.log._lock for log access")
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
        PrintStyle.info("[pf_reply] 🔍 process_chain_end fired")

        if not self.agent or self.agent.number != 0:
            return

        context = self.agent.context
        pf_routing = context.data.get("pf_routing")
        if not pf_routing:
            PrintStyle.info("[pf_reply] No pf_routing metadata, skipping")
            return

        PrintStyle.info(f"[pf_reply] 📤 Processing reply for {pf_routing.get('messenger', '?')} → {pf_routing.get('contact_id', '?')}")

        # Cancel any active progress check-in timer
        progress_task = context.data.pop("pf_progress_task", None)
        if progress_task and not progress_task.done():
            progress_task.cancel()
            PrintStyle.info("[pf_reply] Progress timer cancelled")

        # Graceful steering: discard response and dispatch most recent queued message
        steer_queue = context.data.get("pf_steer_queue", [])
        if steer_queue:
            # Take the most recent message from the queue
            steer_msg = steer_queue.pop()
            # Clear the queue — older messages are already visible in UI via mq.log_user_message()
            context.data["pf_steer_queue"] = []

            PrintStyle.info(
                f"[pf_reply] 🔄 Steered: discarding response, "
                f"dispatching most recent of {len(steer_queue) + 1} queued messages"
            )
            context.log.log(
                type="info",
                content=f"🔄 Steered: previous response discarded for new message ({len(steer_queue) + 1} messages were queued).",
            )

            # Dispatch the most recent queued message
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
            last_err: Exception | None = None
            for attempt in (1, 2):
                try:
                    result = await asyncio.to_thread(
                        send_message,
                        contact_id,
                        chunk,
                        messenger,
                        group_id,
                    )
                    # Inspect response for partial failures (HTTP 202 can still have failedMessages)
                    failed = result.get("failedMessages", [])
                    if failed:
                        for fm in failed:
                            err = fm.get("error", "unknown error")
                            PrintStyle.error(
                                f"[pf_reply] ⚠️ Send failed for contact {fm.get('contactId', '?')}: {err}"
                            )
                        last_err = Exception(
                            f"{len(failed)} message(s) failed: "
                            + ", ".join(fm.get("error", "unknown") for fm in failed)
                        )
                    else:
                        PrintStyle.info(
                            f"[pf_reply] ✅ Sent response chunk ({messenger}, {len(chunk)} chars)"
                        )
                        last_err = None
                    break
                except Exception as e:
                    last_err = e
                    if attempt == 1:
                        await asyncio.sleep(1)

            if last_err:
                PrintStyle.error(
                    f"[pf_reply] Failed to send chunk after retry: {format_error(last_err)}"
                )
                context.log.log(
                    type="error",
                    content=f"⚠️ Failed to send reply to {messenger} "
                    f"(chunk dropped): {last_err}",
                )

        # Clear routing metadata after send
        context.data.pop("pf_routing", None)
