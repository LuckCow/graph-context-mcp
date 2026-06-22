"""SessionPersister tests (WP3): debounced flush + lenient load."""

from __future__ import annotations

from typing import Any

from graph_context.application.session_persister import SessionPersister
from graph_context.domain.session import FocusEntry, FocusStack, SessionState
from graph_context.infrastructure.memory.fake_session_store import InMemorySessionStore


def _focused(project: str, *node_ids: str) -> SessionState:
    state = SessionState(project=project)
    for node_id in node_ids:
        state.focus.push(node_id)
    return state


async def test_flush_is_debounced_to_every_n_mutations() -> None:
    store = InMemorySessionStore()
    session = _focused("Ashfall", "a")
    persister = SessionPersister(store, session, flush_every=3)

    await persister.note_mutation()
    await persister.note_mutation()
    assert await store.load() is None  # not yet -- below the threshold

    await persister.note_mutation()  # third mutation crosses the threshold
    assert await store.load() is not None


async def test_explicit_flush_always_saves() -> None:
    store = InMemorySessionStore()
    persister = SessionPersister(store, _focused("Ashfall", "a"), flush_every=100)
    await persister.flush()
    assert await store.load() is not None


async def test_round_trip_through_store() -> None:
    store = InMemorySessionStore()
    session = _focused("Ashfall", "a", "b")
    session.focus.pin("a")
    await SessionPersister(store, session).flush()

    restored = await SessionPersister.load_or_fresh(store, SessionState())
    assert restored.project == "Ashfall"
    assert [(e.node_id, e.pinned) for e in restored.focus.entries] == [
        (e.node_id, e.pinned) for e in session.focus.entries
    ]
    assert restored.recent.items == session.recent.items


async def test_load_or_fresh_returns_fresh_when_empty() -> None:
    fresh = SessionState(project="Fresh")
    restored = await SessionPersister.load_or_fresh(InMemorySessionStore(), fresh)
    assert restored is fresh


async def test_load_or_fresh_degrades_on_unreadable_store() -> None:
    class Boom:
        async def load(self) -> dict[str, Any] | None:
            raise RuntimeError("store on fire")

        async def save(self, snapshot: dict[str, Any]) -> None:  # pragma: no cover
            ...

    fresh = SessionState(project="Fresh")
    restored = await SessionPersister.load_or_fresh(Boom(), fresh)
    assert restored is fresh  # never crashes startup


async def test_from_snapshot_is_lenient_about_partial_data() -> None:
    # A snapshot missing fields / with junk focus entries degrades field by
    # field rather than raising.
    snapshot = {"project": "P", "focus": [{"pinned": True}], "recent": ["r1", "r2"]}
    state = SessionState.from_snapshot(snapshot)
    assert state.project == "P"
    assert state.focus.entries == ()           # the id-less focus entry is dropped
    assert state.recent.items == ("r1", "r2")


def test_focus_restore_round_trips() -> None:
    stack = FocusStack.restore([FocusEntry("x", pinned=True), FocusEntry("y")])
    assert stack.top == "x"
    assert [(e.node_id, e.pinned) for e in stack.entries] == [("x", True), ("y", False)]
