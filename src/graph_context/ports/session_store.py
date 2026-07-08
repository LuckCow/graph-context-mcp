"""SessionStore port: keyed persistence for session working state.

Session state (working set / scratchpad / mode -- ``SessionState``) is
persisted per SESSION, addressed by an explicit key, so every chat,
channel, or client keeps its own stable context (WP8, ADR 021). The port
deals in the plain-dict snapshots produced by ``SessionState.to_snapshot()``;
implementations know nothing about working-set semantics.

Contract:
* ``key`` is a non-empty transport-scoped session id (``"mcp"``, ``"cli"``,
  ``"anytype:<chat_id>"``, ``"discord:<channel_id>"``). There is no
  unkeyed session and no default: an empty key raises ``ValueError`` --
  it is a programming error, never data.
* Distinct keys are fully independent snapshots.
* ``load`` returns ``None`` when no snapshot exists for the key yet.
  Implementations must treat unreadable/corrupt stored state as ``None``
  (log a warning) -- a broken snapshot must never prevent startup.
* ``save`` overwrites the key's snapshot; last write wins. Callers are
  expected to debounce (see ``application/session_persister.py``) --
  implementations should not.
* I/O failures (store unreachable, backend errors) must surface as
  ``GraphContextError`` subclasses -- that is what the lenient-load path
  in ``SessionPersister`` catches. Anything else is treated as a bug and
  propagates.
"""

from __future__ import annotations

from typing import Any, Protocol


class SessionStore(Protocol):
    async def load(self, key: str) -> dict[str, Any] | None: ...

    async def save(self, snapshot: dict[str, Any], key: str) -> None: ...


def require_session_key(key: str) -> str:
    """Shared guard: an empty/blank session key is a bug, not a request."""
    if not key or not key.strip():
        raise ValueError("session key must be a non-empty string")
    return key
