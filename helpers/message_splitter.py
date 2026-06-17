"""
Message splitting utility for messenger length limits.
"""

MESSENGER_LENGTH_LIMITS = {
    "signal": 2000,
    "simplex": 8000,
    "session": 2000,
}


def split_message(text: str, messenger: str) -> list[str]:
    """Split a long message into chunks that fit within messenger limits."""
    limit = MESSENGER_LENGTH_LIMITS.get(messenger, 2000)
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text

    while len(remaining) > limit:
        split_at = remaining.rfind("\n\n", 0, limit)
        if split_at == -1:
            split_at = remaining.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at == -1 or split_at == 0:
            split_at = limit
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()

    if remaining:
        chunks.append(remaining)

    return chunks
