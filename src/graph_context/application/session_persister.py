"""Debounced session persistence (WP3).

Policy (settled in WORK_PACKAGES): never flush per-touch -- the focus
stack changes on every read. Flush on (a) every N mutations, (b) project
switch, (c) server shutdown. The tool layer calls ``note_mutation()``
after each write-ish operation and ``flush()`` from the lifespan teardown.

Loading is lenient by contract about *expected* trouble -- a store that
cannot be reached (``GraphContextError``, the SessionStore error contract)
or a snapshot whose shape is corrupt degrades to the provided fresh state
with a logged warning. Programming errors are not caught: they should
crash startup loudly instead of silently discarding the user's session.
"""

from __future__ import annotations

import logging

from graph_context.domain.session import SessionState
from graph_context.errors import GraphContextError
from graph_context.ports.session_store import SessionStore

logger = logging.getLogger(__name__)

DEFAULT_FLUSH_EVERY = 10


class SessionPersister:
    def __init__(
        self, store: SessionStore, session: SessionState, *, flush_every: int = DEFAULT_FLUSH_EVERY
    ) -> None:
        self._store = store
        self._session = session
        self._flush_every = flush_every
        self._mutations_since_flush = 0

    @classmethod
    async def load_or_fresh(cls, store: SessionStore, fresh: SessionState) -> SessionState:
        """Restore a session snapshot if one exists and parses; else ``fresh``."""
        try:
            snapshot = await store.load()
        except GraphContextError:  # lenient-load contract (store unreachable)
            logger.warning("session store unreadable; starting fresh", exc_info=True)
            return fresh
        if snapshot is None:
            return fresh
        try:
            return SessionState.from_snapshot(snapshot)
        except (AttributeError, KeyError, TypeError, ValueError):  # corrupt shape
            logger.warning("corrupt session snapshot; starting fresh", exc_info=True)
            return fresh

    async def note_mutation(self) -> None:
        self._mutations_since_flush += 1
        if self._mutations_since_flush >= self._flush_every:
            await self.flush()

    async def flush(self) -> None:
        await self._store.save(self._session.to_snapshot())
        self._mutations_since_flush = 0
