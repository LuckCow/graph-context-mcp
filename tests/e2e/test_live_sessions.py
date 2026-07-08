"""Live keyed sessions (WP8): two chats, two independent contexts.

Pins against a real server what the contract suite asserts mock-backed:
each session key owns its own gc_session_context node (discriminated by
the gc_session_key property), distinct keys keep independent scratchpad /
working set / mode, and a fresh store + registry -- the "restart" --
restores each key's snapshot. The WP8-named test: "two sessions mutate
focus independently; restart restores both". Gated by ANYTYPE_E2E=1.

NOTE: the session-scoped ``live_config`` resets the GC-E2E space before
and after the run.
"""

from __future__ import annotations

from graph_context.application.session_registry import SessionRegistry
from graph_context.domain.models import Detail
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.config import AnytypeConfig
from graph_context.infrastructure.anytype.session_repository import AnytypeSessionStore

KEY_A = "anytype:chat-arc-one"
KEY_B = "anytype:chat-arc-two"


class TestLiveKeyedSessions:
    async def test_two_sessions_persist_and_restore_independently(
        self, live_config: AnytypeConfig
    ) -> None:
        client = AnytypeClient(live_config)
        try:
            labels = {KEY_A: "Arc One", KEY_B: "Arc Two"}
            store = AnytypeSessionStore(client, labels=labels)
            registry = SessionRegistry(store)

            session_a, persister_a = await registry.get(KEY_A)
            session_a.scratchpad = "arc one: the siege"
            session_a.working_set.hold("node-1", Detail.FULL)
            session_a.mode = "authoring"
            await persister_a.flush()

            session_b, persister_b = await registry.get(KEY_B)
            session_b.scratchpad = "arc two: the exile"
            session_b.mode = "world_modeling"
            await persister_b.flush()

            # The "restart": fresh store + registry, no cache, find by key.
            reopened = SessionRegistry(AnytypeSessionStore(client))
            restored_a, _ = await reopened.get(KEY_A)
            restored_b, _ = await reopened.get(KEY_B)

            assert restored_a.scratchpad == "arc one: the siege"
            assert restored_a.mode == "authoring"
            assert [e.node_id for e in restored_a.working_set.entries] == ["node-1"]
            assert restored_b.scratchpad == "arc two: the exile"
            assert restored_b.mode == "world_modeling"
            assert restored_b.working_set.entries == ()  # never clobbered by A
        finally:
            await client.aclose()
