"""WP14 composition-root wiring: startup failure modes + the serve loop.

Uses the MockAnytype chat routes end-to-end for the transport side and an
in-memory runtime (scripted driver) for the turn side -- the same split
the live bot has: transport clients and repository clients are separate.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from graph_context.domain.models import NodeDraft
from graph_context.domain.session import SessionState
from graph_context.errors import GraphContextError
from graph_context.infrastructure.anytype.chat import AnytypeChatClient
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.config import AnytypeConfig
from graph_context.infrastructure.anytype.mock_server import MockAnytype
from graph_context.infrastructure.memory.fake_repository import InMemoryGraphRepository
from graph_context.interface.profiles import get_profile
from graph_context.interface.tools import build_services
from graph_context.orchestrator import bootstrap
from graph_context.orchestrator.anytype_chat_bot import (
    _catch_up,
    _serve_chat,
    _watch_chats,
    _watch_graph,
)
from graph_context.orchestrator.anytype_chat_transport import (
    AnytypeChatTurnHandler,
    ChatCursor,
)
from graph_context.orchestrator.channels import ChannelRoute
from graph_context.orchestrator.drivers import LLMTurn, ScriptedDriver
from graph_context.orchestrator.modes import load_registry
from graph_context.orchestrator.pipeline import Orchestrator
from graph_context.orchestrator.spaces import SpaceBinding

FICTION = get_profile("fiction")


async def _noop_lister(binding: object) -> list[tuple[str, str]]:
    return []


class TestStartup:
    async def test_startup_fails_loudly_when_gc_spaces_file_is_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GC_SPACES_FILE", raising=False)
        with pytest.raises(GraphContextError, match="GC_SPACES_FILE"):
            await bootstrap.build_space_runtimes(_noop_lister)


def _wired_chat(
    mock: MockAnytype, turns: list[LLMTurn], cursor: ChatCursor
) -> tuple[AnytypeChatClient, str, AnytypeChatTurnHandler]:
    chat_id = mock.seed_chat("General")
    config = AnytypeConfig(api_key="test", space_id=mock.space_id)
    chat_client = AnytypeChatClient(AnytypeClient(config, transport=mock.transport))
    services = build_services(
        InMemoryGraphRepository(role_overrides=FICTION.role_overrides),
        SessionState(project="Ashfall"),
    )
    orchestrator = Orchestrator(
        services=services, driver=ScriptedDriver(turns),
        profile=FICTION, registry=load_registry(FICTION),
    )
    handler = AnytypeChatTurnHandler(
        routes={chat_id: ChannelRoute(orchestrator=orchestrator)},
        spaces={chat_id: mock.space_id},
        cursor=cursor,
    )
    return chat_client, chat_id, handler


SPACE = "bafyspacealphaalphaalphaalpha"


def _spaces_file(tmp_path: Path, body: str = "profile = \"fiction\"") -> str:
    path = tmp_path / "spaces.toml"
    path.write_text(f'[spaces."{SPACE}"]\n{body}\n')
    return str(path)


class TestServeAllAndDiscovery:
    """WP8: one runtime per space, every chat its own keyed session, and
    the live-discovery watcher registers new chats without a restart."""

    async def test_all_chats_share_one_route_minus_the_exclusions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GC_BACKEND", "memory")
        monkeypatch.setenv("GC_SPACES_FILE", _spaces_file(
            tmp_path, 'profile = "fiction"\nexclude_chats = ["chat-c"]'
        ))
        monkeypatch.setenv("GC_DRIVER", "manual")

        async def lister(binding: object) -> list[tuple[str, str]]:
            return [("chat-a", "Plot"), ("chat-b", "Characters"), ("chat-c", "Scratch")]

        runtimes = await bootstrap.build_space_runtimes(lister)
        try:
            assert set(runtimes.routes) == {"chat-a", "chat-b"}  # c excluded
            assert runtimes.routes["chat-a"] is runtimes.routes["chat-b"]  # one route
            assert runtimes.session_labels[SPACE] == {
                "anytype:chat-a": "Plot", "anytype:chat-b": "Characters",
            }
        finally:
            await composition_teardown(runtimes)

    async def test_register_chat_is_visible_to_an_existing_handler(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GC_BACKEND", "memory")
        monkeypatch.setenv("GC_SPACES_FILE", _spaces_file(tmp_path))
        monkeypatch.setenv("GC_DRIVER", "manual")

        async def lister(binding: object) -> list[tuple[str, str]]:
            return [("chat-a", "Plot")]

        runtimes = await bootstrap.build_space_runtimes(lister)
        try:
            # The handler is constructed with the runtime's live maps.
            handler = AnytypeChatTurnHandler(
                routes=runtimes.routes, spaces=runtimes.spaces
            )
            assert "chat-late" not in handler.routes
            bootstrap.register_chat(runtimes, SPACE, "chat-late", "Late Thread")
            # Aliased maps: the addition is visible without rebuilding.
            assert handler.routes["chat-late"] is runtimes.space_routes[SPACE]
            assert handler.spaces["chat-late"] == SPACE
        finally:
            await composition_teardown(runtimes)


async def composition_teardown(runtimes: bootstrap.SpaceRuntimes) -> None:
    from graph_context import composition

    await composition.run_teardown(runtimes.teardown)


class TestLiveDiscovery:
    async def test_a_chat_created_after_startup_gets_served(self) -> None:
        """The headline WP8 feature: no restart to serve a new thread."""
        mock = MockAnytype()
        config = AnytypeConfig(api_key="test", space_id=mock.space_id)
        chat_client = AnytypeChatClient(AnytypeClient(config, transport=mock.transport))
        binding = SpaceBinding(space_id=mock.space_id, profile=FICTION)
        route = ChannelRoute(orchestrator=Orchestrator(
            services=build_services(
                InMemoryGraphRepository(role_overrides=FICTION.role_overrides),
                SessionState(project="Ashfall"),
            ),
            driver=ScriptedDriver([LLMTurn(reply="served the new thread")]),
            profile=FICTION, registry=load_registry(FICTION),
        ))
        runtimes = bootstrap.SpaceRuntimes(
            routes={}, spaces={}, descriptions={}, help_line="",
            teardown=[], space_routes={mock.space_id: route},
            space_bindings={mock.space_id: binding},
            session_labels={mock.space_id: {}},
        )
        handler = AnytypeChatTurnHandler(routes=runtimes.routes, spaces=runtimes.spaces)

        class _Done(Exception):
            """Raised to unwind the TaskGroup (cancels the watcher + serve
            tasks it spawned) once the assertions have run."""

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(_watch_chats(
                    handler, chat_client, binding, runtimes, tg, interval=0.02
                ))
                # No chat at startup; create one mid-run.
                await asyncio.sleep(0.05)
                assert runtimes.routes == {}
                chat_id = mock.seed_chat("A New Arc")
                # The watcher discovers + serves it; a message gets answered.
                async with asyncio.timeout(5):
                    while chat_id not in runtimes.routes:
                        await asyncio.sleep(0.01)
                    await asyncio.sleep(0.05)  # serve task's catch-up settles
                    mock.post_chat_message_directly(chat_id, "human", "hello?")
                    while True:  # placeholder first, then the edit lands
                        replies = [
                            m for m in mock._chat_messages[chat_id]
                            if m["creator"] == mock.api_member_id
                        ]
                        if any(
                            m["content"]["text"] == "served the new thread"
                            for m in replies
                        ):
                            break
                        await asyncio.sleep(0.01)
                assert runtimes.spaces[chat_id] == mock.space_id
                assert replies[0]["content"]["text"] == "served the new thread"
                raise _Done
        except* _Done:
            pass


class TestPeriodicGraphResync:
    async def test_out_of_band_edits_reach_the_index_without_a_turn(
        self,
    ) -> None:
        """A human edits the space in the Anytype UI while the bot idles;
        the watcher pulls the change into the shared index (the duplicate-
        Garden failure never gets a chance to happen)."""

        repository = InMemoryGraphRepository()
        route = ChannelRoute(orchestrator=Orchestrator(
            services=build_services(repository, SessionState(project="Todo")),
            driver=ScriptedDriver([]),
            profile=FICTION, registry=load_registry(FICTION),
        ))
        repository.stage_out_of_band(
            NodeDraft("Project", name="Garden", summary="Yard work.")
        )
        watcher = asyncio.ensure_future(
            _watch_graph(route, "space-1", interval=0.01)
        )
        try:
            async with asyncio.timeout(5):
                while not repository.graph.find_by_name("Garden"):
                    await asyncio.sleep(0.01)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)


class TestServeLoop:
    async def test_a_streamed_message_round_trips_to_a_posted_reply(self) -> None:
        mock = MockAnytype()
        chat_client, chat_id, handler = _wired_chat(
            mock, [LLMTurn(reply="hello from the graph")], ChatCursor()
        )
        serve = asyncio.ensure_future(
            _serve_chat(handler, chat_client, chat_id, handler.cursor)
        )
        try:
            await asyncio.sleep(0.05)  # catch-up done, stream open
            mock.post_chat_message_directly(chat_id, "human", "hi bot")
            # The first bot post is the "Processing…" placeholder; wait for
            # the edit that turns it into the reply.
            async with asyncio.timeout(5):
                while True:
                    texts = [
                        m["content"]["text"]
                        for m in mock._chat_messages[chat_id]
                        if m["creator"] == mock.api_member_id
                    ]
                    if "hello from the graph" in texts:
                        break
                    await asyncio.sleep(0.01)
            assert texts == ["hello from the graph"]  # edited in place
            # The bot's own reply echoed back on the stream but ran no turn
            # (echo suppression) -- give it a beat to prove it.
            await asyncio.sleep(0.05)
            assert len(mock._chat_messages[chat_id]) == 2  # hi + one reply
        finally:
            serve.cancel()
            await asyncio.gather(serve, return_exceptions=True)

    async def test_offline_messages_are_answered_on_startup(
        self, tmp_path: Path
    ) -> None:
        mock = MockAnytype()
        cursor_path = str(tmp_path / "cursor.json")
        # A previous life: the cursor points at the first message.
        primer = ChatCursor(cursor_path)
        chat_client, chat_id, handler = _wired_chat(
            mock, [LLMTurn(reply="caught up")], ChatCursor(cursor_path)
        )
        seen_id = mock.post_chat_message_directly(chat_id, "human", "seen before")
        seen_order = mock._chat_messages[chat_id][-1]["order_id"]
        primer.fast_forward(chat_id, seen_order)
        mock.post_chat_message_directly(chat_id, "human", "sent while offline")
        # Reload the persisted position, then catch up.
        handler.cursor = ChatCursor(cursor_path)
        await _catch_up(handler, chat_client, chat_id, handler.cursor)
        replies = [
            m["content"]["text"]
            for m in mock._chat_messages[chat_id]
            if m["creator"] == mock.api_member_id
        ]
        assert replies == ["caught up"]  # one turn: the offline message only
        assert seen_id  # the pre-cursor message ran no turn

    async def test_a_first_run_chat_skips_its_history(self) -> None:
        mock = MockAnytype()
        chat_client, chat_id, handler = _wired_chat(
            mock, [LLMTurn(reply="should not fire")], ChatCursor()
        )
        mock.post_chat_message_directly(chat_id, "human", "ancient history")
        await _catch_up(handler, chat_client, chat_id, handler.cursor)
        replies = [
            m for m in mock._chat_messages[chat_id]
            if m["creator"] == mock.api_member_id
        ]
        assert replies == []
        assert handler.cursor.has(chat_id)  # positioned past the history


class TestScheduledEventWatcher:
    """ADR 027: the third watcher fires due Scheduled Events as turns."""

    async def test_a_due_event_fires_a_turn_into_the_chat_once(self) -> None:
        from graph_context.domain import scheduling
        from graph_context.orchestrator.anytype_chat_bot import _watch_schedule

        mock = MockAnytype()
        chat_client, chat_id, handler = _wired_chat(
            mock, [LLMTurn(reply="Reminder: taxes are due April 15.")],
            ChatCursor(),
        )
        route = handler.routes[chat_id]
        repository = route.orchestrator.services.repository
        await repository.create_node(NodeDraft(
            type=scheduling.SCHEDULED_TYPE_KEY, name="tax reminder",
            summary="s",
            fields={
                scheduling.FIELD_SCHEDULE: "2020-01-01T09:00",  # long due
                scheduling.FIELD_PROMPT: "Remind Nick about taxes.",
                scheduling.FIELD_SESSION_KEY: f"anytype:{chat_id}",
            },
        ))
        watcher = asyncio.ensure_future(_watch_schedule(
            handler, chat_client, route, mock.space_id, interval=0.01
        ))
        try:
            async with asyncio.timeout(5):
                while True:
                    texts = [
                        m["content"]["text"]
                        for m in mock._chat_messages[chat_id]
                        if m["creator"] == mock.api_member_id
                    ]
                    if "Reminder: taxes are due April 15." in texts:
                        break
                    await asyncio.sleep(0.01)
            # A few more ticks must not re-fire the spent one-shot (the
            # scripted driver has no second turn: a re-fire would post an
            # error message).
            await asyncio.sleep(0.05)
            bot_posts = [
                m["content"]["text"]
                for m in mock._chat_messages[chat_id]
                if m["creator"] == mock.api_member_id
            ]
            assert bot_posts == ["Reminder: taxes are due April 15."]
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    async def test_a_ui_created_recurring_event_is_armed_without_a_post(
        self,
    ) -> None:
        from graph_context.domain import scheduling
        from graph_context.orchestrator.anytype_chat_bot import _watch_schedule

        mock = MockAnytype()
        chat_client, chat_id, handler = _wired_chat(mock, [], ChatCursor())
        route = handler.routes[chat_id]
        repository = route.orchestrator.services.repository
        node = await repository.create_node(NodeDraft(
            type=scheduling.SCHEDULED_TYPE_KEY, name="weekly review",
            summary="s",
            fields={
                scheduling.FIELD_SCHEDULE: "0 9 * * 1",
                scheduling.FIELD_PROMPT: "Review the backlog.",
            },
        ))
        watcher = asyncio.ensure_future(_watch_schedule(
            handler, chat_client, route, mock.space_id, interval=0.01
        ))
        try:
            async with asyncio.timeout(5):
                while not repository.graph.node(node.id).fields.get(
                    scheduling.FIELD_LAST_FIRED
                ):
                    await asyncio.sleep(0.01)
            assert mock._chat_messages[chat_id] == []  # armed, not fired
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
