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

from graph_context.domain import attribution
from graph_context.domain.session import SessionState
from graph_context.infrastructure.memory.fake_repository import InMemoryGraphRepository
from graph_context.interface.profiles import get_profile
from graph_context.interface.services import build_services
from graph_context.orchestrator.anytype_chat_transport import (
    MAX_ATTACHMENTS,
    MAX_IMAGE_BYTES,
    MAX_TEXT_BYTES,
    MAX_TITLE_CHARS,
    PROCESSING_NOTICE,
    AnytypeChatTurnHandler,
    ChatCursor,
    ChatTitler,
    InboundAttachment,
    InboundChatMessage,
    SentMessages,
    TurnReply,
    attachment_note,
    classify_attachment,
    fenced_file,
    object_references,
    plainify,
)
from graph_context.orchestrator.channels import ChannelRoute
from graph_context.orchestrator.drivers import (
    LLMTurn,
    ScriptedDriver,
    ToolCall,
    TranscriptEvent,
)
from graph_context.orchestrator.pipeline import Orchestrator, ReplyEvent
from graph_context.orchestrator.turn_activity import ChatActivity

FICTION = get_profile("fiction")
SPACE = "bafyspacealphaalphaalphaalpha"
CHAT = "bafychatalphaalphaalphaalpha2"
OBJECT_ID = "bafyreidzubmznaff57mj4wxefz7s4s2qbc4lzowv2qskqjd2m5667smi5a"


def _route(
    turns: list[LLMTurn] | None = None,
    driver: ScriptedDriver | None = None,
) -> ChannelRoute:
    from tests.orchestrator.mode_fixtures import fiction_registry

    services = build_services(
        InMemoryGraphRepository(role_overrides=FICTION.role_overrides),
        SessionState(project="Ashfall"),
    )
    orchestrator = Orchestrator(
        services=services, driver=driver or ScriptedDriver(turns or []),
        profile=FICTION, registry=fiction_registry(),
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


class _ChatRecorder:
    """send/edit fakes over an in-memory message list, so assertions read
    the chat as a user would see it (edits applied in place, C8:
    attachments replaced wholesale)."""

    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []
        self.posted: list[str] = []  # send texts, pre-edit, in post order
        self.edited: list[str] = []  # ids that received an edit
        self.deleted: list[str] = []  # ids removed from the chat

    async def send(self, text: str, attachments: tuple[str, ...] = ()) -> str:
        message_id = f"sent-{len(self.posted) + 1}"
        self.messages.append(
            {"id": message_id, "text": text, "attachments": attachments}
        )
        self.posted.append(text)
        return message_id

    async def edit(
        self, message_id: str, text: str, attachments: tuple[str, ...] = ()
    ) -> None:
        message = next(m for m in self.messages if m["id"] == message_id)
        message["text"] = text
        message["attachments"] = attachments
        self.edited.append(message_id)

    async def delete(self, message_id: str) -> None:
        self.messages = [m for m in self.messages if m["id"] != message_id]
        self.deleted.append(message_id)

    def texts(self) -> list[str]:
        return [str(m["text"]) for m in self.messages]

    def attachments(self) -> list[object]:
        return [m["attachments"] for m in self.messages]


async def _run(
    handler: AnytypeChatTurnHandler,
    message: InboundChatMessage,
    recorder: _ChatRecorder | None = None,
) -> _ChatRecorder:
    """One run_turn through a handler-wired reply; returns its recorder."""
    recorder = recorder or _ChatRecorder()
    await handler.run_turn(
        message, handler.reply(recorder.send, recorder.edit)
    )
    return recorder


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


class _TranscriptRecordingDriver(ScriptedDriver):
    """Scripted, but keeps what the pipeline SHOWED it at each decision."""

    def __init__(self, turns: list[LLMTurn]) -> None:
        super().__init__(turns)
        self.transcripts: list[tuple[TranscriptEvent, ...]] = []

    async def decide(
        self, transcript, tools, goal: str = "", *, options=None,
    ) -> LLMTurn:
        self.transcripts.append(tuple(transcript))
        return await super().decide(transcript, tools, goal)


class TestTurn:
    async def test_the_senders_display_name_reaches_the_model(self) -> None:
        """The API names every message's creator; the model must see it,
        or 'assign this to me'-shaped requests are unanswerable."""
        driver = _TranscriptRecordingDriver([LLMTurn(reply="hi Nick")])
        handler = AnytypeChatTurnHandler(
            routes={CHAT: _route(driver=driver)}, spaces={CHAT: SPACE}
        )
        await _run(handler, _message(creator_name="Nick"))
        (transcript,) = driver.transcripts
        assert transcript[-1].text == "[from Nick] hello"

    async def test_one_message_becomes_one_turn_with_anytype_scoped_ids(self) -> None:
        route = _route([LLMTurn(reply="hi there")])
        handler = AnytypeChatTurnHandler(
            routes={CHAT: route}, spaces={CHAT: SPACE}
        )
        recorder = await _run(handler, _message())
        assert recorder.texts() == ["hi there"]
        assert route.orchestrator.mode_of(f"anytype:{CHAT}") == "world_modeling"

    async def test_every_posted_id_is_recorded_for_echo_suppression(self) -> None:
        handler = _handler([LLMTurn(reply="a\n" + "b" * 2500)])
        recorder = await _run(handler, _message())
        assert len(recorder.messages) > 1  # chunked
        for message in recorder.messages:  # the placeholder's id included
            assert str(message["id"]) in handler.sent

    async def test_referenced_objects_ride_the_first_chunk_as_attachments(
        self,
    ) -> None:
        reply = f"made [Mira]({OBJECT_ID})\n" + "pad " * 700
        handler = _handler([LLMTurn(reply=reply)])
        recorder = await _run(handler, _message())
        assert len(recorder.messages) > 1  # chunked
        # C8: the first chunk EDITS the placeholder, so its cards must
        # ride the edit body -- the edit is a wholesale replacement.
        assert recorder.attachments()[0] == (OBJECT_ID,)
        assert all(a == () for a in recorder.attachments()[1:])
        assert "[Mira](" not in recorder.texts()[0]  # plainified: name only

    async def test_explicit_attach_merges_with_scraped_references(
        self,
    ) -> None:
        """ADR 038: ``ReplyEvent.attach`` (the turn's intent card) rides
        ahead of ids scraped from the text, deduped, first chunk only --
        the same C7 card surface either way."""
        from graph_context.orchestrator.pipeline import ReplyEvent

        handler = _handler([])
        recorder = _ChatRecorder()
        reply = handler.reply(recorder.send, recorder.edit)
        intent_id = OBJECT_ID.replace("bafyreid", "bafyreie")
        await handler.deliver_events(
            [ReplyEvent(
                f"made [Mira]({OBJECT_ID})\n" + "pad " * 700,
                attach=(intent_id, OBJECT_ID),  # OBJECT_ID also in text
            )],
            reply,
        )
        assert len(recorder.messages) > 1  # chunked
        assert recorder.attachments()[0] == (intent_id, OBJECT_ID)  # deduped
        assert all(a == () for a in recorder.attachments()[1:])

    async def test_a_processed_message_is_not_eligible_twice(self) -> None:
        handler = _handler([LLMTurn(reply="once")])
        message = _message()
        assert handler.accepts(message)
        await _run(handler, message)
        assert not handler.accepts(message)  # replay after reconnect

    async def test_concurrent_messages_in_one_chat_serialize_on_the_route_lock(
        self,
    ) -> None:
        handler = _handler([LLMTurn(reply="first"), LLMTurn(reply="second")])
        recorder = _ChatRecorder()
        await asyncio.gather(
            _run(handler, _message(message_id="m1", order_id="o5"), recorder),
            _run(handler, _message(message_id="m2", order_id="o6"), recorder),
        )
        assert sorted(recorder.texts()) == ["first", "second"]


class TestProcessingPlaceholder:
    """The turn posts a visible placeholder at once, then EDITS it into
    the real reply -- the user sees progress instead of silence while
    turns (which serialize per space) run.

    Since WP19 this lifecycle is the NO-ACTIVITY contract: it is what a
    turn without a live-activity sink (and, via the sink's inertness, a
    mode whose ``activity_detail`` is ``off``) must keep doing
    bit-for-bit."""

    async def test_the_placeholder_posts_first_and_becomes_the_reply(
        self,
    ) -> None:
        handler = _handler([LLMTurn(reply="done thinking")])
        recorder = await _run(handler, _message())
        assert recorder.posted == [PROCESSING_NOTICE]  # the only raw send
        assert recorder.texts() == ["done thinking"]  # edited in place
        assert recorder.edited == ["sent-1"]

    async def test_later_chunks_post_as_ordinary_messages(self) -> None:
        handler = _handler([LLMTurn(reply="a\n" + "b" * 2500)])
        recorder = await _run(handler, _message())
        assert recorder.posted[0] == PROCESSING_NOTICE
        assert recorder.edited == ["sent-1"]  # only the placeholder
        assert len(recorder.messages) > 1

    async def test_a_delivery_without_a_placeholder_degrades_to_a_send(
        self,
    ) -> None:
        # The composition root's error path when open() itself failed.
        sent = SentMessages()
        recorder = _ChatRecorder()
        reply = TurnReply(send=recorder.send, edit=recorder.edit, sent=sent)
        await reply.deliver("[error] boom")
        assert recorder.texts() == ["[error] boom"]
        assert "sent-1" in sent  # error posts feed echo suppression too

    async def test_command_turns_skip_the_placeholder(self) -> None:
        # /clear (and /mode) are answered instantly by the pipeline; a
        # placeholder would only add a notification, so the output posts
        # alone, fresh.
        recorder = await _run(_handler(), _message(text="/clear"))
        assert PROCESSING_NOTICE not in recorder.posted
        assert recorder.edited == []
        assert len(recorder.messages) == 1

    async def test_an_eventless_turn_does_not_strand_the_placeholder(
        self,
    ) -> None:
        recorder = _ChatRecorder()
        reply = TurnReply(
            send=recorder.send, edit=recorder.edit, sent=SentMessages()
        )
        await reply.open()
        await reply.finish()
        assert recorder.texts() == ["(the turn produced no reply)"]
        assert recorder.edited == ["sent-1"]


async def _run_streaming(
    handler: AnytypeChatTurnHandler, message: InboundChatMessage
) -> _ChatRecorder:
    """run_turn with a live-activity sink, edits uncoalesced for testing."""
    recorder = _ChatRecorder()
    reply = handler.reply(recorder.send, recorder.edit)
    activity = ChatActivity(
        reply=reply, edit=recorder.edit, delete=recorder.delete,
        min_interval=0.0,
    )
    await handler.run_turn(message, reply, activity)
    return recorder


class TestActivityStreaming:
    """WP19 (ADR 029): with a sink attached, the placeholder becomes a
    live activity message edited as the turn runs, the reply posts as a
    fresh message, and the activity message is DELETED once the reply
    is delivered -- the reply alone remains in the chat."""

    async def test_activity_streams_then_is_deleted_and_the_reply_posts_fresh(
        self,
    ) -> None:
        probe = ToolCall("context", {"action": "get"})
        handler = _handler([
            LLMTurn(tool_calls=(probe,)), LLMTurn(reply="the answer"),
        ])
        recorder = await _run_streaming(handler, _message())
        assert recorder.posted[0] == PROCESSING_NOTICE
        # Live edits landed on the placeholder while the turn ran; the
        # reply posted fresh and the trace left the chat.
        assert set(recorder.edited) == {"sent-1"}
        assert len(recorder.edited) >= 2
        assert recorder.deleted == ["sent-1"]
        assert recorder.texts() == ["the answer"]

    async def test_every_message_id_is_echo_suppressed(self) -> None:
        probe = ToolCall("context", {"action": "get"})
        handler = _handler([
            LLMTurn(tool_calls=(probe,)), LLMTurn(reply="done"),
        ])
        recorder = await _run_streaming(handler, _message())
        for message in recorder.messages:
            assert str(message["id"]) in handler.sent
        # The deleted activity message stays in the ledger too: its id
        # was recorded at open() and deletion never rewrites history.
        assert "sent-1" in handler.sent

    async def test_command_turns_post_only_their_output(self) -> None:
        # /mode is answered instantly (no model turn, and it returns
        # before the pipeline's turn_started so the sink stays inert):
        # no placeholder posts, only the notice -- one notification.
        handler = _handler([])
        recorder = await _run_streaming(
            handler, _message(text="/mode authoring")
        )
        assert recorder.edited == []
        assert len(recorder.messages) == 1
        assert "mode switched to authoring" in recorder.texts()[0]

    async def test_a_replyless_streamed_turn_strands_nothing(self) -> None:
        # The driver script runs dry -> budget notice; the activity
        # message is still deleted and nothing strands as "Processing…".
        probe = ToolCall("context", {"action": "get"})
        route = _route([LLMTurn(tool_calls=(probe,))] * 99)
        route.orchestrator.max_tool_calls = 3
        handler = AnytypeChatTurnHandler(
            routes={CHAT: route}, spaces={CHAT: SPACE}
        )
        recorder = await _run_streaming(handler, _message())
        assert recorder.deleted == ["sent-1"]
        assert PROCESSING_NOTICE not in recorder.texts()
        assert "working…" not in " ".join(recorder.texts())


class TestReplyPreparation:
    """Quirk C7: the chat UI is plain text; object references become
    ATTACHMENTS (rendered as cards), not links."""

    def test_referenced_object_ids_are_collected_in_order(self) -> None:
        second = OBJECT_ID.replace("bafyreid", "bafyreie")
        text = f"made [Mira]({OBJECT_ID}) near {second} and {OBJECT_ID} again"
        assert object_references(text) == (OBJECT_ID, second)

    def test_attachments_are_capped(self) -> None:
        # Suffixes stay within base32's [a-z2-7] alphabet.
        ids = [f"bafyreibcdefghijklmnopqrstu{c}" for c in "abcdefghijkl"]
        assert len(object_references(" ".join(ids))) == MAX_ATTACHMENTS

    def test_a_markdown_object_link_collapses_to_its_name(self) -> None:
        assert plainify(f"created [Mira]({OBJECT_ID})") == "created Mira"

    def test_ordinary_links_keep_their_url_in_plain_form(self) -> None:
        assert (
            plainify("see [the docs](https://example.com/x)")
            == "see the docs (https://example.com/x)"
        )

    def test_headers_and_emphasis_are_stripped(self) -> None:
        text = "## Scope\n**101 nodes** across *12* types with `code`"
        assert plainify(text) == "Scope\n101 nodes across 12 types with code"

    def test_bullets_and_plain_text_are_untouched(self) -> None:
        text = "- first thing\n- second thing\n\nplain words"
        assert plainify(text) == text


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

    def test_begin_adopts_a_chat_with_every_message_still_new(
        self, tmp_path: Path
    ) -> None:
        """Live discovery's position: the chat reads as resumed, and the
        messages typed before the subscription opened are the gap."""
        path = str(tmp_path / "cursor.json")
        cursor = ChatCursor(path)
        cursor.begin(CHAT)
        assert cursor.has(CHAT)
        assert cursor.is_new(_message(order_id="o1"))
        reborn = ChatCursor(path)  # persisted: survives a crash unserved
        assert reborn.has(CHAT)

    def test_begin_never_rewinds_an_existing_position(
        self, tmp_path: Path
    ) -> None:
        path = str(tmp_path / "cursor.json")
        cursor = ChatCursor(path)
        cursor.fast_forward(CHAT, "o9")
        cursor.begin(CHAT)
        assert not cursor.is_new(_message(order_id="o9"))


class TestIntentOrigin:
    async def test_a_mutating_turn_records_the_triggering_message(self) -> None:
        from graph_context.application.intent_recorder import IntentRecorder
        from graph_context.application.mutation_journal import MutationJournal
        from graph_context.orchestrator.drivers import ToolCall
        from tests.orchestrator.mode_fixtures import fiction_registry

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
            registry=fiction_registry(),
            provenance=IntentRecorder(repository),
        )
        handler = AnytypeChatTurnHandler(
            routes={CHAT: ChannelRoute(orchestrator=orchestrator)},
            spaces={CHAT: SPACE},
        )
        await _run(handler, _message(message_id="msg-42"))
        intents = [n for n in repository.graph.nodes() if n.type_key == "gc_intent"]
        assert len(intents) == 1
        assert intents[0].fields[attribution.FIELD_ORIGIN] == f"anytype:{CHAT}:msg-42"


class TestClearWatermarkAndSeeding:
    """WP15: /clear is a persisted context boundary; startup seeding
    rebuilds conversation memory from the answered slice of the window."""

    async def test_clear_records_a_persisted_watermark(self, tmp_path: Path) -> None:
        clear_marks = ChatCursor(str(tmp_path / "cleared.json"))
        handler = _handler(clear_marks=clear_marks)
        await _run(handler, _message(text="/clear", order_id="o7"))
        assert not clear_marks.is_new(_message(order_id="o7"))
        assert clear_marks.is_new(_message(order_id="o8"))
        # Persisted: a fresh cursor instance reads the same boundary.
        reloaded = ChatCursor(str(tmp_path / "cleared.json"))
        assert not reloaded.is_new(_message(order_id="o7"))

    async def test_clear_reaches_the_orchestrator_as_a_notice(self) -> None:
        handler = _handler()
        recorder = await _run(handler, _message(text="/clear"))
        assert any("memory cleared" in text for text in recorder.texts())

    def test_seed_events_classifies_and_bounds_the_window(self) -> None:
        sent = SentMessages()
        sent.add("bot-1")
        cursor = ChatCursor()
        cursor.fast_forward(CHAT, "o6")  # everything <= o6 was answered
        clear_marks = ChatCursor()
        clear_marks.fast_forward(CHAT, "o2")  # a /clear happened at o2
        handler = _handler(
            sent=sent, cursor=cursor, clear_marks=clear_marks,
            bot_identity="botIdent",
        )
        window = [
            _message(message_id="h0", order_id="o1", text="before the clear"),
            _message(message_id="h1", order_id="o3", text="hi bot"),
            _message(message_id="bot-1", order_id="o4", text="hello human"),
            _message(message_id="h2", order_id="o5", text="/mode authoring"),
            _message(
                message_id="b2", order_id="o5x", text="by identity",
                creator=f"_participant_{SPACE}_botIdent",
            ),
            _message(message_id="h3", order_id="o7", text="unanswered backlog"),
        ]
        assert handler.seed_events(CHAT, window) == [
            ("user", "hi bot"),            # after the clear, answered
            ("assistant", "hello human"),  # ours via the sent ledger
            ("assistant", "by identity"),  # ours via the identity suffix
        ]

    def test_seed_events_without_a_clear_takes_the_whole_answered_window(
        self,
    ) -> None:
        cursor = ChatCursor()
        cursor.fast_forward(CHAT, "o9")
        handler = _handler(cursor=cursor)
        window = [_message(message_id="h1", order_id="o3", text="hello")]
        assert handler.seed_events(CHAT, window) == [("user", "hello")]

    def test_seed_events_attribute_user_messages_like_live_turns(self) -> None:
        """Restart-seeded history must read identically to remembered
        history: user messages carry their sender's display name."""
        cursor = ChatCursor()
        cursor.fast_forward(CHAT, "o9")
        handler = _handler(cursor=cursor)
        window = [_message(
            message_id="h1", order_id="o3", text="hello", creator_name="Nick",
        )]
        assert handler.seed_events(CHAT, window) == [
            ("user", "[from Nick] hello")
        ]

    def test_seed_events_keeps_the_reply_posted_after_the_cursor(self) -> None:
        """The reply to the last answered message always lands AFTER it, so
        its order_id sits above the cursor -- but it is ours, not unanswered
        backlog (the gate never re-answers our own posts). Dropping it made
        every restart-seeded prompt end in an apparently-unanswered user
        message, so the model re-served the previous request each turn
        (dogfooding 2026-07-11)."""
        sent = SentMessages()
        sent.add("bot-2")
        cursor = ChatCursor()
        cursor.fast_forward(CHAT, "o6")  # the last ANSWERED user message
        handler = _handler(sent=sent, cursor=cursor)
        window = [
            _message(message_id="h1", order_id="o6", text="add the pottery task"),
            _message(message_id="bot-2", order_id="o7", text="Created it."),
            _message(message_id="h2", order_id="o8", text="true backlog"),
        ]
        assert handler.seed_events(CHAT, window) == [
            ("user", "add the pottery task"),
            ("assistant", "Created it."),
        ]


# -- scheduled turns (WP18, ADR 027) -----------------------------------------


class TestTargetChat:
    def test_the_events_own_chat_wins_when_served_in_the_space(self) -> None:
        handler = _handler()
        assert handler.target_chat(SPACE, f"anytype:{CHAT}") == CHAT

    def test_foreign_or_missing_keys_fall_back_to_the_first_served_chat(
        self,
    ) -> None:
        handler = _handler()
        for key in ("", "mcp", "discord:123", "anytype:not-served"):
            assert handler.target_chat(SPACE, key) == CHAT

    def test_a_chat_key_from_another_space_does_not_cross_spaces(self) -> None:
        other_chat, other_space = "bafychatbetabetabetabetabeta2", "space-b"
        route = _route()
        handler = AnytypeChatTurnHandler(
            routes={CHAT: route, other_chat: route},
            spaces={CHAT: SPACE, other_chat: other_space},
        )
        assert handler.target_chat(other_space, f"anytype:{CHAT}") == other_chat

    def test_a_space_with_no_served_chat_returns_none(self) -> None:
        handler = _handler()
        assert handler.target_chat("space-without-chats", "") is None


class TestScheduledTurn:
    async def _seed_event(self, handler: AnytypeChatTurnHandler):
        from graph_context.domain import scheduling
        from graph_context.domain.models import NodeDraft

        repository = handler.routes[CHAT].orchestrator.services.repository
        node = await repository.create_node(NodeDraft(
            type=scheduling.SCHEDULED_TYPE_KEY, name="tax reminder",
            summary="s",
            fields={
                scheduling.FIELD_SCHEDULE: "2026-01-01T09:00",
                scheduling.FIELD_PROMPT: "Remind Nick about taxes.",
                scheduling.FIELD_SESSION_KEY: f"anytype:{CHAT}",
            },
        ))
        return repository, node

    async def test_the_model_wakes_with_the_stored_prompt(self) -> None:
        from graph_context.application.scheduler import DueEvent

        driver = _TranscriptRecordingDriver([LLMTurn(reply="On it.")])
        handler = _handler(routes={CHAT: _route(driver=driver)})
        repository, node = await self._seed_event(handler)
        recorder = _ChatRecorder()
        due = DueEvent(
            node_id=node.id, name="tax reminder",
            prompt="Remind Nick about taxes.",
            session_key=f"anytype:{CHAT}",
        )
        await handler.run_scheduled(
            CHAT, due, handler.reply(recorder.send, recorder.edit)
        )
        prompt_event = driver.transcripts[0][-1]
        assert prompt_event.kind == "user"
        assert "[scheduled event 'tax reminder' fired]" in prompt_event.text
        assert "Remind Nick about taxes." in prompt_event.text
        assert "not by a user message" in prompt_event.text

    async def test_no_placeholder_posts_only_the_finished_reply(self) -> None:
        # Nobody is waiting on a turn they didn't start, so nothing
        # appears in the chat until the reply is ready -- no
        # "Processing" placeholder, one message, echo-suppressed.
        from graph_context.application.scheduler import DueEvent

        handler = _handler([LLMTurn(reply="On it.")])
        repository, node = await self._seed_event(handler)
        recorder = _ChatRecorder()
        due = DueEvent(
            node_id=node.id, name="tax reminder", prompt="p",
            session_key=f"anytype:{CHAT}",
        )
        await handler.run_scheduled(
            CHAT, due, handler.reply(recorder.send, recorder.edit)
        )
        assert recorder.posted == ["On it."]
        assert recorder.edited == []  # nothing to edit: no placeholder
        assert all(m["id"] in handler.sent for m in recorder.messages)

    async def test_the_event_is_marked_fired_even_when_the_turn_fails(
        self,
    ) -> None:
        from graph_context.application.scheduler import DueEvent
        from graph_context.domain import scheduling

        class _ExplodingDriver(ScriptedDriver):
            async def decide(self, transcript, tools, goal, *, options=None):  # type: ignore[override]
                raise RuntimeError("driver down")

        handler = _handler(routes={CHAT: _route(driver=_ExplodingDriver([]))})
        repository, node = await self._seed_event(handler)
        recorder = _ChatRecorder()
        due = DueEvent(
            node_id=node.id, name="tax reminder", prompt="p",
            session_key=f"anytype:{CHAT}",
        )
        raised = False
        try:
            await handler.run_scheduled(
                CHAT, due, handler.reply(recorder.send, recorder.edit)
            )
        except RuntimeError:
            raised = True  # the composition root's error posture owns it
        assert raised
        stored = repository.graph.node(node.id)
        assert stored.fields.get(scheduling.FIELD_LAST_FIRED)  # at-most-once


class TestChatTitler:
    """WP21 (ADR 031): the pure titling policy -- untitled test, one
    attempt per chat, defensive title shaping."""

    def test_untitled_names_need_a_title_and_real_names_do_not(self) -> None:
        titler = ChatTitler(names={
            "c1": "", "c2": "Chat", "c3": "New chat", "c4": "Trip planning",
        })
        assert titler.needs_title("c1")
        assert titler.needs_title("c2")
        assert titler.needs_title("c3")
        assert not titler.needs_title("c4")
        assert titler.needs_title("unknown-chat")  # no name on record yet

    def test_an_attempt_is_spent_win_or_lose(self) -> None:
        titler = ChatTitler(names={"c1": ""})
        titler.mark_attempted("c1")
        assert not titler.needs_title("c1")

    def test_recording_a_title_updates_the_shared_names(self) -> None:
        names: dict[str, str] = {"c1": ""}
        titler = ChatTitler(names=names)
        titler.record("c1", "Siege Engines 101")
        assert names["c1"] == "Siege Engines 101"  # aliased, not copied

    def test_sanitize_strips_wrappers_and_trailing_punctuation(self) -> None:
        assert ChatTitler.sanitize('"Siege Engines 101."') == "Siege Engines 101"
        assert ChatTitler.sanitize("**Bold title**") == "Bold title"
        assert ChatTitler.sanitize("  Title:\nexplanation line") == "Title"
        assert ChatTitler.sanitize("many   inner\tspaces") == "many inner spaces"

    def test_sanitize_caps_length_at_a_word_boundary(self) -> None:
        long = "word " * 40
        title = ChatTitler.sanitize(long)
        assert len(title) <= MAX_TITLE_CHARS
        assert not title.endswith(" ")

    def test_sanitize_of_nothing_usable_is_empty(self) -> None:
        assert ChatTitler.sanitize("") == ""
        assert ChatTitler.sanitize("\n\n  \n") == ""

    def test_prompt_events_snip_long_inputs(self) -> None:
        (event,) = ChatTitler.prompt_events("u" * 2000, "r" * 2000)
        assert event.kind == "user"
        assert len(event.text) < 1200  # both sides snipped
        assert "…" in event.text


class TestAttachmentClassification:
    """WP23: the pure inbound policy -- what to do with an attachment,
    from pre-download facts alone."""

    def test_images_within_the_cap_are_images(self) -> None:
        assert classify_attachment("image", 1024, "png") == "image"
        assert classify_attachment("image", MAX_IMAGE_BYTES + 1, "png") == "stub"

    def test_text_files_go_by_extension_and_size(self) -> None:
        assert classify_attachment("file", 100, "csv") == "text"
        assert classify_attachment("file", 100, ".MD") == "text"  # normalized
        assert classify_attachment("file", MAX_TEXT_BYTES + 1, "csv") == "stub"
        assert classify_attachment("file", 100, "pdf") == "stub"
        assert classify_attachment("file", 100, "") == "stub"

    def test_media_and_plain_objects_split(self) -> None:
        assert classify_attachment("video", 100, "mp4") == "stub"
        assert classify_attachment("audio", 100, "mp3") == "stub"
        assert classify_attachment("page", 0, "") == "object"
        assert classify_attachment("task", 0, "") == "object"

    def test_fences_and_notes_shape(self) -> None:
        assert fenced_file("a.csv", "x,y") == '<file name="a.csv">\nx,y\n</file>'
        note = attachment_note("big.csv", 999, "too large to read here")
        assert "big.csv" in note and "999" in note and "too large" in note


class TestAttachmentGate:
    def test_a_bare_file_drop_is_a_turn(self) -> None:
        handler = AnytypeChatTurnHandler(
            routes={"c1": ChannelRoute(orchestrator=None)},  # type: ignore[arg-type]
            spaces={"c1": "s1"},
        )
        with_file = InboundChatMessage(
            space_id="s1", chat_id="c1", message_id="m1", creator="human",
            text="", order_id="!!a",
            attachments=(InboundAttachment(target="f1", type="file"),),
        )
        empty = InboundChatMessage(
            space_id="s1", chat_id="c1", message_id="m2", creator="human",
            text="  ", order_id="!!b",
        )
        assert handler.accepts(with_file)
        assert not handler.accepts(empty)


class TestFileDelivery:
    """WP23: file reply events post as real uploads when the reply has a
    send_file primitive, and degrade to fenced text without one."""

    @staticmethod
    def _reply(sent_files: list, sent_texts: list, with_primitive: bool):
        async def send(text, attachments=()):
            sent_texts.append(text)
            return f"m{len(sent_texts)}"

        async def edit(message_id, text, attachments=()):
            sent_texts.append(f"edit:{text}")

        async def send_file(name, content):
            sent_files.append((name, content))
            return f"f{len(sent_files)}"

        return TurnReply(
            send=send, edit=edit, sent=SentMessages(),
            send_file=send_file if with_primitive else None,
        )

    async def test_a_file_event_uses_the_primitive(self) -> None:
        files: list = []
        texts: list = []
        reply = self._reply(files, texts, with_primitive=True)
        handler = AnytypeChatTurnHandler(routes={}, spaces={})
        await handler.deliver_events(
            [ReplyEvent("the answer"),
             ReplyEvent("a,b\n1,2", kind="file", file_name="data.csv")],
            reply,
        )
        assert files == [("data.csv", "a,b\n1,2")]
        assert texts == ["the answer"]  # content never doubles as text

    async def test_without_the_primitive_files_degrade_to_fences(self) -> None:
        files: list = []
        texts: list = []
        reply = self._reply(files, texts, with_primitive=False)
        handler = AnytypeChatTurnHandler(routes={}, spaces={})
        await handler.deliver_events(
            [ReplyEvent("a,b", kind="file", file_name="data.csv")], reply
        )
        assert files == []
        assert texts and "data.csv" in texts[0] and "a,b" in texts[0]

    async def test_delivered_file_messages_join_the_echo_ledger(self) -> None:
        files: list = []
        reply = self._reply(files, [], with_primitive=True)
        await reply.deliver_file("x.md", "hi")
        assert "f1" in reply.sent
