"""SessionRegistry: the one place live sessions come from (WP8, ADR 021).

Every session -- a chat thread, a Discord channel, the CLI, the MCP
client -- is addressed by an explicit transport-scoped key and owns its
own ``SessionState`` + debounced ``SessionPersister`` over the keyed
``SessionStore``. The registry lazily loads each key's snapshot on first
request (lenient, per ``SessionPersister.load_or_fresh``), caches the
pair for the process lifetime, and flushes every live session at
teardown. There is no unkeyed or "default" session anywhere.
"""

from __future__ import annotations

import asyncio

from graph_context.application.session_persister import (
    DEFAULT_FLUSH_EVERY,
    SessionPersister,
)
from graph_context.domain.session import SessionState
from graph_context.ports.session_store import SessionStore, require_session_key


class SessionRegistry:
    def __init__(
        self,
        store: SessionStore,
        *,
        default_project: str | None = None,
        flush_every: int = DEFAULT_FLUSH_EVERY,
    ) -> None:
        self._store = store
        self._default_project = default_project
        self._flush_every = flush_every
        self._sessions: dict[str, tuple[SessionState, SessionPersister]] = {}
        # One lock around first-load: two concurrent first turns for the
        # same key must resolve to ONE session object, not two racing loads.
        self._load_lock = asyncio.Lock()

    async def get(self, key: str) -> tuple[SessionState, SessionPersister]:
        key = require_session_key(key)
        if key in self._sessions:
            return self._sessions[key]
        async with self._load_lock:
            if key not in self._sessions:  # lost the race: someone loaded it
                session = await SessionPersister.load_or_fresh(
                    self._store, SessionState(project=self._default_project), key
                )
                persister = SessionPersister(
                    self._store, session, key, flush_every=self._flush_every
                )
                self._sessions[key] = (session, persister)
        return self._sessions[key]

    async def flush_all(self) -> None:
        """Teardown: persist every session this process touched."""
        for _, persister in self._sessions.values():
            await persister.flush()

    def __len__(self) -> int:
        return len(self._sessions)
