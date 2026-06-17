"""
PrivacyFlow Channel Receive Endpoint

POST /api/pf_channel_receive

Receives messages from the PrivacyFlow bridge service and forwards them
to the appropriate Agent Zero context. Uses API key auth (no CSRF/session).

Request body:
{
    "text": "message text",
    "contact_id": "sender identifier",
    "group_id": "group identifier (optional, for group messages)",
    "messenger": "signal" | "simplex" | "session"
}

Response:
{
    "context_id": "agent zero context id",
    "accepted": true
}
"""

from helpers.api import ApiHandler, Request, Response
from agent import AgentContext, UserMessage
from helpers.print_style import PrintStyle
import os


class PfChannelReceive(ApiHandler):
    """Receive messages from PrivacyFlow bridge service."""

    @classmethod
    def requires_api_key(cls) -> bool:
        return True

    @classmethod
    def requires_auth(cls) -> bool:
        return False

    @classmethod
    def requires_csrf(cls) -> bool:
        return False

    @classmethod
    def get_methods(cls) -> list[str]:
        return ["POST"]

    async def process(self, input: dict, request: Request) -> dict | Response:
        text = input.get("text", "")
        contact_id = input.get("contact_id", "")
        group_id = input.get("group_id")
        messenger = input.get("messenger", "")

        if not text:
            return Response("Missing required field: text", status=400)
        if not contact_id:
            return Response("Missing required field: contact_id", status=400)
        if not messenger:
            return Response("Missing required field: messenger", status=400)

        # Build context mapping key: group_id for groups, contact_id for DMs
        mapping_key = group_id if group_id else contact_id

        # Look up existing context by checking all contexts for matching PF routing data
        context = None
        for ctx in AgentContext.all():
            pf_routing = ctx.data.get("pf_routing")
            if pf_routing and pf_routing.get("mapping_key") == mapping_key:
                context = ctx
                break

        # Create new context if none found
        if not context:
            from initialize import initialize_agent
            context = AgentContext(
                config=initialize_agent(),
                set_current=False,
            )
            PrintStyle.info(f"[pf_channel] Created new context {context.id} for {mapping_key}")

        # Store routing metadata in context.data for process_chain_end extension
        context.data["pf_routing"] = {
            "contact_id": contact_id,
            "group_id": group_id,
            "messenger": messenger,
            "mapping_key": mapping_key,
        }

        # Send message to agent
        msg = UserMessage(message=text, id="")
        context.communicate(msg)

        PrintStyle.info(
            f"[pf_channel] ✅ Forwarded message to context {context.id} "
            f"({messenger}, {'group' if group_id else 'DM'})"
        )

        return {
            "context_id": context.id,
            "accepted": True,
        }
