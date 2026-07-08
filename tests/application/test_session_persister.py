"""SessionPersister tests (WP3): debounced flush + lenient load."""

from __future__ import annotations

from typing import Any

import pytest

from graph_context.application.session_persister import SessionPersister
from graph_context.domain.models import Detail
from graph_context.domain.session import SessionState, WorkingSet, WorkingSetEntry
from graph_context.errors import GraphContextError
from graph_context.infrastructure.memory.fake_session_store import InMemorySessionStore


def _holding(project: str, *node_ids: str) -> SessionState:
    state = SessionState(project=project)
    for node_id in node_ids:
        state.working_set.hold(node_id)
    return state


async def test_flush_is_debounced_to_every_n_mutations() -> None:
    store = InMemorySessionStore()
    session = _holding("Ashfall", "a")
    persister = SessionPersister(store, session, flush_every=3)

    await persister.note_mutation()
    await persister.note_mutation()
    assert await store.load() is None  # not yet -- below the threshold

    await persister.note_mutation()  # third mutation crosses the threshold
    assert await store.load() is not None


async def test_explicit_flush_always_saves() -> None:
    store = InMemorySessionStore()
    persister = SessionPersister(store, _holding("Ashfall", "a"), flush_every=100)
    await persister.flush()
    assert await store.load() is not None


async def test_round_trip_through_store() -> None:
    store = InMemorySessionStore()
    session = _holding("Ashfall", "a", "b")
    session.working_set.hold("a", Detail.FULL)
    session.scratchpad = "next: the siege aftermath"
    await SessionPersister(store, session).flush()

    restored = await SessionPersister.load_or_fresh(store, SessionState())
    assert restored.project == "Ashfall"
    assert restored.working_set.entries == session.working_set.entries
    assert restored.scratchpad == session.scratchpad
    assert restored.recent.items == session.recent.items


async def test_load_or_fresh_returns_fresh_when_empty() -> None:
    fresh = SessionState(project="Fresh")
    restored = await SessionPersister.load_or_fresh(InMemorySessionStore(), fresh)
    assert restored is fresh


async def test_load_or_fresh_degrades_on_unreadable_store() -> None:
    # The port contract: I/O failures surface as GraphContextError.
    class Boom:
        async def load(self) -> dict[str, Any] | None:
            raise GraphContextError("store on fire")

        async def save(self, snapshot: dict[str, Any]) -> None:  # pragma: no cover
            ...

    fresh = SessionState(project="Fresh")
    restored = await SessionPersister.load_or_fresh(Boom(), fresh)
    assert restored is fresh  # never crashes startup


async def test_load_or_fresh_propagates_programming_errors() -> None:
    # A bug in a store must crash loudly, not silently discard the session.
    class Buggy:
        async def load(self) -> dict[str, Any] | None:
            raise RuntimeError("a bug, not an I/O failure")

        async def save(self, snapshot: dict[str, Any]) -> None:  # pragma: no cover
            ...

    with pytest.raises(RuntimeError):
        await SessionPersister.load_or_fresh(Buggy(), SessionState())


async def test_from_snapshot_is_lenient_about_partial_data() -> None:
    # A snapshot missing fields / with junk working-set entries degrades
    # field by field rather than raising.
    snapshot = {
        "project": "P",
        "working_set": [{"detail": "full"}],
        "recent": ["r1", "r2"],
    }
    state = SessionState.from_snapshot(snapshot)
    assert state.project == "P"
    assert state.working_set.entries == ()     # the id-less entry is dropped
    assert state.recent.items == ("r1", "r2")


def test_working_set_restore_round_trips() -> None:
    restored = WorkingSet.restore(
        [WorkingSetEntry("x", Detail.FULL), WorkingSetEntry("y")]
    )
    assert restored.top == "x"
    assert restored.entries == (
        WorkingSetEntry("x", Detail.FULL),
        WorkingSetEntry("y", Detail.SUMMARIES),
    )
