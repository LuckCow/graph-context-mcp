"""Transport-neutral reply rendering shared by chat transports (WP14).

Extracted from ``discord_transport`` when the Anytype chat transport
arrived: prefixing reply kinds and chunking long text are dialect shims
every chat surface needs, not Discord policy. ``discord_transport``
re-exports both, so its public surface is unchanged.
"""

from __future__ import annotations

from graph_context.orchestrator.pipeline import ReplyEvent

DEFAULT_MESSAGE_LIMIT = 2000

# The one in-chat notice for an unexpected turn crash (both bots): the
# details go to the log, never the chat.
TURN_FAILED_NOTICE = "[error] the turn failed; see the bot log for the traceback"

_PREFIXES = {"reply": "", "notice": "[notice] ", "error": "[error] "}


def render(event: ReplyEvent) -> str:
    """Transport-neutral event -> chat text (plain prefixes, like the CLI)."""
    return f"{_PREFIXES[event.kind]}{event.text}"


def chunk(text: str, limit: int = DEFAULT_MESSAGE_LIMIT) -> list[str]:
    """Split into sendable pieces, preferring line then word boundaries."""
    text = text.strip()
    pieces: list[str] = []
    while len(text) > limit:
        window = text[: limit + 1]
        cut = window.rfind("\n")
        if cut <= 0:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
        pieces.append(text[:cut].rstrip())
        text = text[cut:].strip()
    if text:
        pieces.append(text)
    return pieces
