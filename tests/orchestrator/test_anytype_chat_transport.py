"""WP14 Anytype chat transport policy: gate, cursor, deep links -- no httpx.

The whole per-message policy is plain logic in ``anytype_chat_transport``
(only the ``anytype_chat_bot`` composition root touches infrastructure;
import-linter holds the line), so these tests run against in-memory
runtimes with a scripted driver, mirroring ``test_discord_transport``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from graph_context.domain.session import SessionState
from graph_context.infrastructure.memory.fake_repository import InMemoryGraphRepository
from graph_context.interface.profiles import get_profile
from graph_context.interface.tools import build_services
from graph_context.orchestrator.anytype_chat_transport import (
    AnytypeChatTurnHandler,
    ChatCursor,
    InboundChatMessage,
    SentMessages,
    linkify,
)
from graph_context.orchestrator.channels import ChannelRoute
from graph_context.orchestrator.drivers import LLMTurn, ScriptedDriver
from graph_context.orchestrator.pipeline import Orchestrator

FICTION = get_profile("fiction")
SPACE = "bafyspacealphaalphaalphaalpha"
CHAT = "bafychatalphaalphaalphaalpha2"
OBJECT_ID = "bafyreidzubmznaff57mj4wxefz7s4s2qbc4lzowv2qskqjd2m5667smi5a"


def _route(turns: list[LLMTurn] | None = None) -> ChannelRoute:
    from graph_context.orchestrator.modes import load_registry

    services = build_services(
        InMemoryGraphRepository(role_overrides=FICTION.role_overrides),
        SessionState(project="Ashfall"),
    )
    orchestrator = Orchestrator(
        services=services, driver=ScriptedDriver(turns or []),
        profile=FICTION, registry=load_registry(FICTION),
    )
    return ChannelRoute(orchestrator=orchestrator)


def _handler(
    turns: list[LLMTurn] | None = None, **overrides: object
) -> AnytypeChatTurnHandler:
    kwargs: dict = dict(routes={CHAT: _route(turns)}, spaces={CHAT: SPACE})
    kwargs.update(overrides)
    return AnytypeChatTurnHandler(**kwargs)


def _message(**overrides: object) -> InboundChatMessage:
    base: dict = dict(
        space_id=SPACE, chat_id=CHAT, message_id="m1",
        creator="member-a", text="hello", order_id="o5",
    )
    base.update(overrides)
    return InboundChatMessage(**base)


class _SendRecorder:
    """A ``send`` fake returning fresh message ids like the live API."""

    def __init__(self) -> None:
        self.pieces: list[str] = []

    async def __call__(self, piece: str) -> str:
        self.pieces.append(piece)
        return f"sent-{len(self.pieces)}"


class TestGate:
    def test_a_member_message_on_a_bound_chat_is_accepted(self) -> None:
        assert _handler().accepts(_message())

    def test_unbound_chats_are_ignored(self) -> None:
        assert not _handler().accepts(_message(chat_id="bafyother"))

    def test_empty_text_is_ignored(self) -> None:
        assert not _handler().accepts(_message(text="   "))

    def test_the_bots_own_posted_messages_are_ignored(self) -> None:
        sent = SentMessages()
        sent.add("m1")
        assert not _handler(sent=sent).accepts(_message(message_id="m1"))

    def test_the_bots_identity_is_ignored_even_for_unposted_messages(self) -> None:
        # Member ids are space-scoped but END with the account identity
        # (quirk C6), so the self-check is a suffix match per space.
        handler = _handler(bot_identity="A73WbotIdentity")
        assert not handler.accepts(
            _message(creator=f"_participant_{SPACE}_A73WbotIdentity")
        )
        assert handler.accepts(
            _message(creator=f"_participant_{SPACE}_AA5KhumanIdentity")
        )

    def test_an_empty_bot_identity_never_suffix_matches_everything(self) -> None:
        # str.endswith("") is True -- the guard must not fire when the
        # identity is unknown (desktop endpoint, shared account).
        assert _handler(bot_identity="").accepts(_message(creator="anyone"))

    def test_backlog_at_or_below_the_cursor_is_ignored(self) -> None:
        cursor = ChatCursor()
        cursor.fast_forward(CHAT, "o5")
        handler = _handler(cursor=cursor)
        assert not handler.accepts(_message(order_id="o4"))
        assert not handler.accepts(_message(order_id="o5"))
        assert handler.accepts(_message(order_id="o6"))


class TestTurn:
    async def test_one_message_becomes_one_turn_with_anytype_scoped_ids(self) -> None:
        route = _route([LLMTurn(reply="hi there")])
        handler = AnytypeChatTurnHandler(
            routes={CHAT: route}, spaces={CHAT: SPACE}
        )
        send = _SendRecorder()
        await handler.run_turn(_message(), send)
        assert send.pieces == ["hi there"]
        assert route.orchestrator.mode_of(f"anytype:{CHAT}") == "world_modeling"

    async def test_every_sent_id_is_recorded_for_echo_suppression(self) -> None:
        handler = _handler([LLMTurn(reply="a\n" + "b" * 2500)])
        send = _SendRecorder()
        await handler.run_turn(_message(), send)
        assert len(send.pieces) > 1  # chunked
        for i in range(len(send.pieces)):
            assert f"sent-{i + 1}" in handler.sent

    async def test_a_processed_message_is_not_eligible_twice(self) -> None:
        handler = _handler([LLMTurn(reply="once")])
        send = _SendRecorder()
        message = _message()
        assert handler.accepts(message)
        await handler.run_turn(message, send)
        assert not handler.accepts(message)  # replay after reconnect

    async def test_concurrent_messages_in_one_chat_serialize_on_the_route_lock(
        self,
    ) -> None:
        handler = _handler([LLMTurn(reply="first"), LLMTurn(reply="second")])
        send = _SendRecorder()
        await asyncio.gather(
            handler.run_turn(_message(message_id="m1", order_id="o5"), send),
            handler.run_turn(_message(message_id="m2", order_id="o6"), send),
        )
        assert sorted(send.pieces) == ["first", "second"]


class TestLinkify:
    def test_a_markdown_link_to_a_bare_object_id_becomes_a_deep_link(self) -> None:
        text = f"created [Mira]({OBJECT_ID})"
        assert linkify(text, SPACE) == (
            f"created [Mira](anytype://object?objectId={OBJECT_ID}"
            f"&spaceId={SPACE})"
        )

    def test_bare_object_ids_become_deep_links(self) -> None:
        out = linkify(f"see {OBJECT_ID} for details", SPACE)
        assert f"[{OBJECT_ID[:8]}…](anytype://object?objectId={OBJECT_ID}" in out

    def test_existing_anytype_links_pass_through_untouched(self) -> None:
        link = f"anytype://object?objectId={OBJECT_ID}&spaceId={SPACE}"
        text = f"already linked: [Mira]({link})"
        assert linkify(text, SPACE) == text

    def test_ordinary_text_and_urls_are_untouched(self) -> None:
        text = "plain words, a [web link](https://example.com), and basferry"
        assert linkify(text, SPACE) == text


class TestSentLedger:
    def test_posted_ids_survive_a_restart_via_the_persisted_file(
        self, tmp_path: Path
    ) -> None:
        """Live-caught bug: on the desktop endpoint the bot posts as the
        user's own account, so after a restart only the persisted ledger
        stops startup catch-up from answering the bot's own old reply."""
        path = str(tmp_path / "sent.json")
        first = SentMessages(path=path)
        first.add("old-reply")
        reborn = SentMessages(path=path)
        assert "old-reply" in reborn
        handler = _handler(sent=reborn)
        assert not handler.accepts(_message(message_id="old-reply"))

    def test_the_ledger_is_bounded(self, tmp_path: Path) -> None:
        path = str(tmp_path / "sent.json")
        ledger = SentMessages(max_size=3, path=path)
        for i in range(5):
            ledger.add(f"m{i}")
        reborn = SentMessages(path=path)
        assert "m0" not in reborn and "m4" in reborn

    def test_an_unreadable_ledger_degrades_to_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "sent.json"
        path.write_text("{not json")
        assert "anything" not in SentMessages(path=str(path))


class TestCursor:
    def test_positions_survive_a_restart_via_the_persisted_file(
        self, tmp_path: Path
    ) -> None:
        path = str(tmp_path / "cursor.json")
        first = ChatCursor(path)
        assert not first.has(CHAT)
        first.fast_forward(CHAT, "o9")
        reborn = ChatCursor(path)
        assert reborn.has(CHAT)
        assert not reborn.is_new(_message(order_id="o9"))
        assert reborn.is_new(_message(order_id="p1"))  # the offline gap

    def test_an_unreadable_file_degrades_to_first_run(self, tmp_path: Path) -> None:
        path = tmp_path / "cursor.json"
        path.write_text("{corrupt")
        cursor = ChatCursor(str(path))
        assert not cursor.has(CHAT)

    def test_advance_only_moves_forward(self, tmp_path: Path) -> None:
        path = str(tmp_path / "cursor.json")
        cursor = ChatCursor(path)
        cursor.fast_forward(CHAT, "o9")
        cursor.fast_forward(CHAT, "o3")  # replayed old event must not rewind
        assert json.loads(Path(path).read_text()) == {CHAT: "o9"}


class TestIntentOrigin:
    async def test_a_mutating_turn_records_the_triggering_message(self) -> None:
        from graph_context.application.intent_recorder import IntentRecorder
        from graph_context.application.mutation_journal import MutationJournal
        from graph_context.orchestrator.drivers import ToolCall
        from graph_context.orchestrator.modes import load_registry

        repository = InMemoryGraphRepository(role_overrides=FICTION.role_overrides)
        journal = MutationJournal()
        services = build_services(
            repository, SessionState(project="Ashfall"), journal=journal
        )
        orchestrator = Orchestrator(
            services=services,
            driver=ScriptedDriver([
                LLMTurn(tool_calls=(ToolCall("create_node", {
                    "type": "Character", "name": "Mira", "summary": "s.",
                }),)),
                LLMTurn(reply="made her"),
            ]),
            profile=FICTION,
            registry=load_registry(FICTION),
            provenance=IntentRecorder(repository),
        )
        handler = AnytypeChatTurnHandler(
            routes={CHAT: ChannelRoute(orchestrator=orchestrator)},
            spaces={CHAT: SPACE},
        )
        await handler.run_turn(_message(message_id="msg-42"), _SendRecorder())
        intents = [n for n in repository.graph.nodes() if n.type_key == "gc_intent"]
        assert len(intents) == 1
        assert intents[0].fields["origin"] == f"anytype:{CHAT}:msg-42"
