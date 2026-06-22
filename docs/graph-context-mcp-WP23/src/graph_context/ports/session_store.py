"""SessionStore port: persistence for the session's working state.

The proposal persists session state as a ``SessionContext`` meta-node so a
restarted server resumes with the same focus stack ("survives restarts,
lays groundwork for multi-user"). The port deals in the plain-dict
snapshots produced by ``SessionState.to_snapshot()``; implementations know
nothing about focus-stack semantics.

Contract:
* ``load`` returns ``None`` when no snapshot exists yet. Implementations
  must treat unreadable/corrupt stored state as ``None`` (log a warning) --
  a broken snapshot must never prevent startup.
* ``save`` overwrites; last write wins. Callers are expected to debounce
  (see ``application/session_persister.py``) -- implementations should not.
"""

from __future__ import annotations

from typing import Any, Protocol


class SessionStore(Protocol):
    async def load(self) -> dict[str, Any] | None: ...

    async def save(self, snapshot: dict[str, Any]) -> None: ...
