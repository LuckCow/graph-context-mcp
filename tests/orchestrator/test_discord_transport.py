"""WP8 Discord transport policy: gate, identity, chunking -- no discord.py.

The whole per-message policy is plain logic in ``discord_transport``, so
these tests run without the discord dependency (only the ``discord_bot``
composition root imports it; import-linter holds the line). The client
shim itself stays untested here, like the CLI loop.
"""

from __future__ import annotations

import asyncio

import pytest

from graph_context.domain.session import SessionState
from graph_context.errors import GraphContextError
from graph_context.infrastructure.memory.fake_repository import InMemoryGraphRepository
from graph_context.interface import tools
from graph_context.interface.profiles import get_profile
from graph_context.interface.services import build_services
from graph_context.orchestrator.channels import ChannelRoute
from graph_context.orchestrator.discord_transport import (
    DISCORD_MESSAGE_LIMIT,
    DiscordTurnHandler,
    InboundMessage,
    chunk,
    parse_channel_allowlist,
    render,
)
from graph_context.orchestrator.drivers import (
    LLMTurn,
    ScriptedDriver,
    TranscriptEvent,
)
from graph_context.orchestrator.pipeline import Orchestrator, ReplyEvent
from tests.orchestrator.mode_fixtures import fiction_registry

FICTION = get_profile("fiction")
ALLOWED_CHANNEL = 1523551542123298896
OTHER_CHANNEL = 1523551542123298897


def _route(turns: list[LLMTurn] | None = None, project: str = "Ashfall") -> ChannelRoute:
    """One channel's runtime over its own in-memory world (ADR 017)."""
    services = build_services(
        InMemoryGraphRepository(role_overrides=FICTION.role_overrides),
        SessionState(project=project),
    )
    orchestrator = Orchestrator(
        services=services, driver=ScriptedDriver(turns or []),
        profile=FICTION, registry=fiction_registry(),
    )
    return ChannelRoute(orchestrator=orchestrator)


def _handler(turns: list[LLMTurn] | None = None) -> DiscordTurnHandler:
    return DiscordTurnHandler(routes={ALLOWED_CHANNEL: _route(turns)})


def _message(**overrides: object) -> InboundMessage:
    base: dict = dict(
        channel_id=ALLOWED_CHANNEL, author_id=42,
        author_is_bot=False, content="hello",
    )
    base.update(overrides)
    return InboundMessage(**base)


class TestGate:
    """WP8 authz stance: unauthorized surfaces run no turns at all."""

    def test_a_human_message_in_an_allowed_channel_is_accepted(self) -> None:
        assert _handler().accepts(_message())

    def test_other_channels_are_ignored(self) -> None:
        assert not _handler().accepts(_message(channel_id=999))

    def test_bot_authors_are_ignored(self) -> None:
        """Covers our own echoes too -- the bot never talks to itself."""
        assert not _handler().accepts(_message(author_is_bot=True))

    def test_empty_content_is_ignored(self) -> None:
        """Empty text in an allowed channel = the message-content intent
        is probably off; gate it out rather than burn a model turn."""
        assert not _handler().accepts(_message(content="   "))


class TestTurn:
    async def test_one_message_becomes_one_turn_and_one_send(self) -> None:
        handler = _handler([LLMTurn(reply="Mira waits at the gate.")])
        sends: list[str] = []

        async def send(text: str) -> None:
            sends.append(text)

        await handler.run_turn(_message(content="Where is Mira?"), send)
        assert sends == ["Mira waits at the gate."]

    async def test_session_is_scoped_to_the_channel(self) -> None:
        """Transport-scoped ids (WP8 settled decision), asserted through
        the public mode API: a /mode switch lands on discord:<channel>."""
        handler = _handler()
        sends: list[str] = []

        async def send(text: str) -> None:
            sends.append(text)

        await handler.run_turn(_message(content="/mode authoring"), send)
        session = f"discord:{ALLOWED_CHANNEL}"
        orchestrator = handler.routes[ALLOWED_CHANNEL].orchestrator
        assert orchestrator.mode_of(session) == "authoring"
        assert sends and sends[0].startswith("[notice] ")

    async def test_the_authors_display_name_reaches_the_model(self) -> None:
        """A shared channel's messages must say who sent them; the pipeline
        prefixes the sender when the transport supplies one."""

        class _RecordingDriver(ScriptedDriver):
            def __init__(self) -> None:
                super().__init__([LLMTurn(reply="hi Nick")])
                self.transcripts: list[tuple[TranscriptEvent, ...]] = []

            async def decide(
                self, transcript, tools, goal: str = "", *, options=None,
            ) -> LLMTurn:
                self.transcripts.append(tuple(transcript))
                return await super().decide(transcript, tools, goal)

        driver = _RecordingDriver()
        services = build_services(
            InMemoryGraphRepository(role_overrides=FICTION.role_overrides),
            SessionState(project="Ashfall"),
        )
        orchestrator = Orchestrator(
            services=services, driver=driver, profile=FICTION,
            registry=fiction_registry(),
        )
        handler = DiscordTurnHandler(
            routes={ALLOWED_CHANNEL: ChannelRoute(orchestrator=orchestrator)}
        )

        async def send(text: str) -> None:
            pass

        await handler.run_turn(_message(author_name="Nick"), send)
        (transcript,) = driver.transcripts
        assert transcript[-1].text == "[from Nick] hello"

    async def test_a_long_reply_is_chunked_under_the_discord_limit(self) -> None:
        handler = _handler([LLMTurn(reply="x" * 2500)])
        sends: list[str] = []

        async def send(text: str) -> None:
            sends.append(text)

        await handler.run_turn(_message(), send)
        assert [len(s) for s in sends] == [2000, 500]

    async def test_concurrent_messages_in_one_channel_run_one_turn_at_a_time(
        self,
    ) -> None:
        """A route's runtime holds one graph-session state, so turns on
        the same channel must not interleave tool calls."""
        active = 0
        overlaps: list[int] = []

        class SlowDriver:
            async def decide(self, transcript, tools, goal="", *, options=None):  # type: ignore[no-untyped-def]
                nonlocal active
                active += 1
                overlaps.append(active)
                await asyncio.sleep(0)  # yield inside the turn
                active -= 1
                return LLMTurn(reply="done")

        handler = _handler()
        handler.routes[ALLOWED_CHANNEL].orchestrator.driver = SlowDriver()

        async def send(text: str) -> None:
            pass

        await asyncio.gather(
            handler.run_turn(_message(content="one"), send),
            handler.run_turn(_message(content="two"), send),
        )
        assert max(overlaps) == 1


class TestRouting:
    """ADR 017: each channel's route is its own world; routes never share
    session state, and only same-route turns serialize."""

    def _two_channel_handler(self) -> DiscordTurnHandler:
        return DiscordTurnHandler(routes={
            ALLOWED_CHANNEL: _route(project="Ashfall"),
            OTHER_CHANNEL: _route(project="Fieldwork"),
        })

    async def test_messages_route_to_their_channels_orchestrator(self) -> None:
        handler = self._two_channel_handler()
        sends: list[str] = []

        async def send(text: str) -> None:
            sends.append(text)

        await handler.run_turn(_message(content="/mode authoring"), send)
        first = handler.routes[ALLOWED_CHANNEL].orchestrator
        second = handler.routes[OTHER_CHANNEL].orchestrator
        assert first.mode_of(f"discord:{ALLOWED_CHANNEL}") == "authoring"
        assert second.mode_of(f"discord:{OTHER_CHANNEL}") != "authoring"

    async def test_work_in_one_channel_never_leaks_into_another(self) -> None:
        """Each route owns its graph and session: a node created (and
        touched) through channel A is invisible to channel B."""
        handler = self._two_channel_handler()
        first = handler.routes[ALLOWED_CHANNEL].orchestrator
        second = handler.routes[OTHER_CHANNEL].orchestrator
        reply = await tools.create_node_tool(
            first.services, type="character", name="Mira", summary="A courier."
        )
        assert "Mira" in reply
        assert first.services.repository.graph.node_count() == 1
        assert second.services.repository.graph.node_count() == 0
        assert second.services.session.working_set.entries == ()

    async def test_turns_in_different_channels_may_interleave(self) -> None:
        active = 0
        overlaps: list[int] = []

        class SlowDriver:
            async def decide(self, transcript, tools, goal="", *, options=None):  # type: ignore[no-untyped-def]
                nonlocal active
                active += 1
                overlaps.append(active)
                await asyncio.sleep(0)  # yield inside the turn
                await asyncio.sleep(0)
                active -= 1
                return LLMTurn(reply="done")

        handler = self._two_channel_handler()
        driver = SlowDriver()
        handler.routes[ALLOWED_CHANNEL].orchestrator.driver = driver
        handler.routes[OTHER_CHANNEL].orchestrator.driver = driver

        async def send(text: str) -> None:
            pass

        await asyncio.gather(
            handler.run_turn(_message(content="one"), send),
            handler.run_turn(_message(channel_id=OTHER_CHANNEL, content="two"), send),
        )
        assert max(overlaps) == 2

    async def test_channels_sharing_one_route_still_serialize(self) -> None:
        """The legacy allowlist maps every channel to one shared route."""
        active = 0
        overlaps: list[int] = []

        class SlowDriver:
            async def decide(self, transcript, tools, goal="", *, options=None):  # type: ignore[no-untyped-def]
                nonlocal active
                active += 1
                overlaps.append(active)
                await asyncio.sleep(0)
                active -= 1
                return LLMTurn(reply="done")

        shared = _route()
        shared.orchestrator.driver = SlowDriver()
        handler = DiscordTurnHandler(
            routes={ALLOWED_CHANNEL: shared, OTHER_CHANNEL: shared}
        )

        async def send(text: str) -> None:
            pass

        await asyncio.gather(
            handler.run_turn(_message(content="one"), send),
            handler.run_turn(_message(channel_id=OTHER_CHANNEL, content="two"), send),
        )
        assert max(overlaps) == 1


class TestChunk:
    def test_short_text_is_a_single_piece(self) -> None:
        assert chunk("hello") == ["hello"]

    def test_whitespace_only_sends_nothing(self) -> None:
        assert chunk("  \n ") == []

    def test_split_prefers_line_breaks(self) -> None:
        text = "a" * 1500 + "\n" + "b" * 1500
        assert chunk(text) == ["a" * 1500, "b" * 1500]

    def test_split_falls_back_to_word_breaks(self) -> None:
        pieces = chunk(("word " * 500).strip())
        assert all(len(p) <= DISCORD_MESSAGE_LIMIT for p in pieces)
        assert all(p.startswith("word") and p.endswith("word") for p in pieces)

    def test_a_wall_of_text_hard_splits_at_the_limit(self) -> None:
        pieces = chunk("x" * 4100)
        assert [len(p) for p in pieces] == [2000, 2000, 100]

    def test_nothing_is_lost_in_the_split(self) -> None:
        text = "\n".join(f"line {i} " + "y" * 90 for i in range(60))
        assert "\n".join(chunk(text)).split() == text.split()


class TestRender:
    def test_replies_are_bare_and_harness_events_are_prefixed(self) -> None:
        assert render(ReplyEvent("hi")) == "hi"
        assert render(ReplyEvent("switched", kind="notice")) == "[notice] switched"
        assert render(ReplyEvent("bad tool", kind="error")) == "[error] bad tool"


class TestAllowlist:
    def test_parses_comma_or_space_separated_ids(self) -> None:
        expected = frozenset({1, 23})
        assert parse_channel_allowlist("1,23") == expected
        assert parse_channel_allowlist("1 23") == expected

    def test_unset_fails_loudly_instead_of_serving_everywhere(self) -> None:
        with pytest.raises(GraphContextError, match="GC_DISCORD_CHANNELS"):
            parse_channel_allowlist(None)

    def test_non_numeric_ids_fail_loudly(self) -> None:
        with pytest.raises(GraphContextError, match="numeric"):
            parse_channel_allowlist("general")
