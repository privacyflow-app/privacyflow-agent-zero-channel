"""
Message splitting utility for messenger length limits.

Agent Zero responses can be long (code blocks, multi-paragraph explanations).
Messengers have different character limits. This module splits long messages
into chunks that fit within each messenger's constraints.
"""

MESSENGER_LENGTH_LIMITS = {
    "signal": 2000,
    "simplex": 8000,
    "session": 2000,
}


def split_message(text: str, messenger: str) -> list[str]:
    """
    Split a long message into chunks that fit within messenger limits.

    Tries to split on natural boundaries (paragraphs, newlines, spaces)
    before falling back to hard character cuts.

    Args:
        text: The message text to split.
        messenger: The messenger protocol (signal, simplex, session).

    Returns:
        List of message chunks, each within the messenger's length limit.
    """
    limit = MESSENGER_LENGTH_LIMITS.get(messenger, 2000)
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text

    while len(remaining) > limit:
        # Try paragraph break first
        split_at = remaining.rfind("\n\n", 0, limit)
        if split_at == -1:
            # Try newline
            split_at = remaining.rfind("\n", 0, limit)
        if split_at == -1:
            # Try space
            split_at = remaining.rfind(" ", 0, limit)
        if split_at == -1 or split_at == 0:
            # Hard cut
            split_at = limit

        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()

    if remaining:
        chunks.append(remaining)

    return chunks
