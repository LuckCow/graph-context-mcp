"""SessionRegistry (WP8): lazy keyed sessions, one object per key."""

from __future__ import annotations

import asyncio

from graph_context.application.session_registry import SessionRegistry
from graph_context.infrastructure.memory.fake_session_store import InMemorySessionStore


async def test_get_is_lazy_and_cached() -> None:
    registry = SessionRegistry(InMemorySessionStore(), default_project="Ashfall")
    session, persister = await registry.get("anytype:a")
    assert session.project == "Ashfall"
    again, _ = await registry.get("anytype:a")
    assert again is session  # same object, not a re-load


async def test_keys_get_independent_sessions() -> None:
    registry = SessionRegistry(InMemorySessionStore())
    first, _ = await registry.get("anytype:a")
    second, _ = await registry.get("anytype:b")
    first.scratchpad = "arc one"
    assert second.scratchpad == ""


async def test_concurrent_first_turns_resolve_to_one_session() -> None:
    registry = SessionRegistry(InMemorySessionStore())
    results = await asyncio.gather(*(registry.get("anytype:a") for _ in range(5)))
    sessions = {id(session) for session, _ in results}
    assert len(sessions) == 1
    assert len(registry) == 1


async def test_flush_all_persists_every_live_session() -> None:
    store = InMemorySessionStore()
    registry = SessionRegistry(store)
    session_a, _ = await registry.get("anytype:a")
    session_b, _ = await registry.get("anytype:b")
    session_a.scratchpad = "arc one"
    session_b.mode = "authoring"
    await registry.flush_all()
    fresh = SessionRegistry(store)  # the "restart"
    restored_a, _ = await fresh.get("anytype:a")
    restored_b, _ = await fresh.get("anytype:b")
    assert restored_a.scratchpad == "arc one"
    assert restored_b.mode == "authoring"


async def test_restored_session_keeps_its_own_project_over_the_default() -> None:
    store = InMemorySessionStore()
    seeded = SessionRegistry(store, default_project="Space Name")
    session, persister = await seeded.get("anytype:a")
    session.project = "Renamed by set_project"
    await persister.flush()
    fresh = SessionRegistry(store, default_project="Space Name")
    restored, _ = await fresh.get("anytype:a")
    assert restored.project == "Renamed by set_project"
