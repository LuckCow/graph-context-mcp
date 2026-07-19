"""Chat client behavior against the mock server (WP14, quirks C1-C5).

The mock pins the live shapes found by spike S10; the live-gated
``tests/e2e/test_live_chat.py`` asserts the same behaviors against a real
server, so these tests double as the mock's fidelity contract.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from graph_context.infrastructure.anytype.chat import (
    AnytypeChatClient,
    ChatEvent,
    parse_sse,
)
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.mock_server import MockAnytype


@pytest.fixture
def chat(client: AnytypeClient) -> AnytypeChatClient:
    return AnytypeChatClient(client)


async def _collect(
    stream: AsyncIterator[ChatEvent], n: int, *, timeout: float = 5.0
) -> list[ChatEvent]:
    events: list[ChatEvent] = []
    async with asyncio.timeout(timeout):
        while len(events) < n:
            events.append(await anext(stream))
    return events


class TestChatRest:
    async def test_send_returns_the_created_message_id(
        self, mock: MockAnytype, chat: AnytypeChatClient
    ) -> None:
        chat_id = mock.seed_chat()
        message_id = await chat.send(chat_id, "hello")
        assert message_id
        stored = await chat._client.list_chat_messages(chat_id)
        assert [m["id"] for m in stored] == [message_id]
        assert stored[0]["content"]["text"] == "hello"

    async def test_messages_are_a_recency_window_oldest_first(
        self, mock: MockAnytype, client: AnytypeClient
    ) -> None:
        chat_id = mock.seed_chat()
        for i in range(5):
            mock.post_chat_message_directly(chat_id, "human", f"m{i}")
        window = await client.list_chat_messages(chat_id, limit=3)
        assert [m["content"]["text"] for m in window] == ["m2", "m3", "m4"]

    async def test_a_429_on_send_retries_with_backoff(
        self, mock: MockAnytype, chat: AnytypeChatClient
    ) -> None:
        chat_id = mock.seed_chat()
        mock.fail_next(1)
        message_id = await chat.send(chat_id, "retried")
        assert message_id  # first attempt 429'd, retry landed

    async def test_attachments_are_sent_as_target_link_envelopes(
        self, mock: MockAnytype, chat: AnytypeChatClient
    ) -> None:
        """Quirk C7: a bare id list 400s live; the envelope form lands."""
        chat_id = mock.seed_chat()
        await chat.send(chat_id, "see these", attachments=("obj-1", "obj-2"))
        (stored,) = await chat._client.list_chat_messages(chat_id)
        assert stored["attachments"] == [
            {"target": "obj-1", "type": "link"},
            {"target": "obj-2", "type": "link"},
        ]

    async def test_an_edit_replaces_text_and_attachments_wholesale(
        self, mock: MockAnytype, chat: AnytypeChatClient
    ) -> None:
        """Quirk C8 (live-confirmed): PATCH is a full content replacement
        -- attachments not re-sent on the edit are removed."""
        chat_id = mock.seed_chat()
        message_id = await chat.send(chat_id, "v1", attachments=("obj-1",))
        await chat.edit(chat_id, message_id, "v2", attachments=("obj-2",))
        (stored,) = await chat._client.list_chat_messages(chat_id)
        assert stored["content"]["text"] == "v2"
        assert stored["attachments"] == [{"target": "obj-2", "type": "link"}]
        await chat.edit(chat_id, message_id, "v3")  # text-only edit
        (stored,) = await chat._client.list_chat_messages(chat_id)
        assert stored["content"]["text"] == "v3"
        assert stored["attachments"] == []  # C8: wiped, not preserved

    async def test_rename_shows_in_the_next_relist(
        self, mock: MockAnytype, chat: AnytypeChatClient
    ) -> None:
        """Quirk C9 (spike S12): the chat namespace has no update route;
        renaming goes through the generic object PATCH and the /chats
        re-list reflects it -- the WP21 auto-titling contract."""
        chat_id = mock.seed_chat(name="")
        await chat.rename(chat_id, "Siege engine logistics")
        assert (chat_id, "Siege engine logistics") in await chat.list_chats()


class TestMarks:
    """Quirk C11 (spike S14): outbound markdown becomes plain text plus
    marks, and the mock enforces the live server's range rules."""

    async def test_send_converts_markdown_links_to_marks(
        self, mock: MockAnytype, chat: AnytypeChatClient
    ) -> None:
        chat_id = mock.seed_chat()
        await chat.send(
            chat_id,
            "See the [API docs](https://developers.anytype.io) for details",
        )
        (stored,) = mock.chat_messages(chat_id)
        assert stored["content"]["text"] == "See the API docs for details"
        assert stored["content"]["marks"] == [{
            "from": 8, "to": 16, "type": "link",
            "param": "https://developers.anytype.io",
        }]

    async def test_plain_text_sends_without_a_marks_key(
        self, mock: MockAnytype, chat: AnytypeChatClient
    ) -> None:
        chat_id = mock.seed_chat()
        await chat.send(chat_id, "no formatting")
        (stored,) = mock.chat_messages(chat_id)
        assert "marks" not in stored["content"]

    async def test_an_edit_replaces_marks_wholesale(
        self, mock: MockAnytype, chat: AnytypeChatClient
    ) -> None:
        """C11d rides C8: the edited body's marks land; an edit whose
        text converts to none drops the old ones."""
        chat_id = mock.seed_chat()
        message_id = await chat.send(chat_id, "[a](https://a.example)")
        await chat.edit(chat_id, message_id, "**bold now**")
        (stored,) = mock.chat_messages(chat_id)
        assert stored["content"]["text"] == "bold now"
        assert stored["content"]["marks"] == [
            {"from": 0, "to": 8, "type": "bold"}
        ]
        await chat.edit(chat_id, message_id, "plain now")
        (stored,) = mock.chat_messages(chat_id)
        assert "marks" not in stored["content"]

    async def test_the_file_caption_travels_through_the_converter(
        self, mock: MockAnytype, chat: AnytypeChatClient
    ) -> None:
        chat_id = mock.seed_chat()
        file_id = await chat.upload_file("report.md", b"# Report")
        await chat.send_file_message(chat_id, "\N{PAPERCLIP} report.md", file_id)
        (stored,) = mock.chat_messages(chat_id)
        assert stored["content"]["text"] == "\N{PAPERCLIP} report.md"
        assert stored["attachments"] == [{"target": file_id, "type": "file"}]

    async def test_the_mock_rejects_ranges_like_the_live_server(
        self, mock: MockAnytype, client: AnytypeClient
    ) -> None:
        """Fidelity pin: out-of-bounds/inverted/negative ranges 500
        (bounds in UTF-16 units -- ``to`` one past an emoji's code-point
        length lands); a non-list ``marks`` 400s."""
        from graph_context.infrastructure.anytype.config import AnytypeApiError

        chat_id = mock.seed_chat()
        emoji_text = "\N{SLIGHTLY SMILING FACE} link"  # 6 cp, 7 utf-16
        ok = await client.create_chat_message(chat_id, {
            "text": emoji_text,
            "marks": [{"from": 2, "to": 7, "type": "link",
                       "param": "https://example.com"}],
        })
        assert ok
        for bad in (
            [{"from": 2, "to": 8, "type": "link"}],   # past utf-16 end
            [{"from": 5, "to": 2, "type": "bold"}],   # inverted
            [{"from": -1, "to": 3, "type": "bold"}],  # negative
            {"from": 0},                              # not a list
        ):
            with pytest.raises(AnytypeApiError):
                await client.create_chat_message(
                    chat_id, {"text": emoji_text, "marks": bad}
                )


class TestSseParsing:
    async def test_backlog_then_live_events_arrive_in_order(
        self, mock: MockAnytype, chat: AnytypeChatClient
    ) -> None:
        chat_id = mock.seed_chat()
        mock.post_chat_message_directly(chat_id, "human", "old 1")
        mock.post_chat_message_directly(chat_id, "human", "old 2")
        stream = chat.stream(chat_id)
        backlog = await _collect(stream, 2)
        mock.post_chat_message_directly(chat_id, "human", "live")
        (live,) = await _collect(stream, 1)
        await stream.aclose()
        assert [e.message.text for e in backlog if e.message] == ["old 1", "old 2"]
        assert live.kind == "message_added" and live.message
        assert live.message.text == "live"
        orders = [e.message.order_id for e in (*backlog, live) if e.message]
        assert orders == sorted(orders)  # C3: string order IS stream order

    async def test_heartbeat_comments_surface_as_keepalive_events(
        self, mock: MockAnytype, chat: AnytypeChatClient
    ) -> None:
        chat_id = mock.seed_chat()
        stream = chat.stream(chat_id)
        first = asyncio.ensure_future(anext(stream))
        await asyncio.sleep(0.01)  # let the request land and register
        mock.emit_chat_heartbeat(chat_id)
        event = await asyncio.wait_for(first, 5)
        await stream.aclose()
        assert event == ChatEvent(kind="heartbeat")

    async def test_message_added_updated_deleted_map_to_events(
        self, mock: MockAnytype, chat: AnytypeChatClient
    ) -> None:
        chat_id = mock.seed_chat()
        stream = chat.stream(chat_id)
        first = asyncio.ensure_future(anext(stream))
        await asyncio.sleep(0.01)
        message_id = await chat.send(chat_id, "v1")
        added = await asyncio.wait_for(first, 5)
        await chat._client.edit_chat_message(chat_id, message_id, {"text": "v2"})
        await chat._client.delete_chat_message(chat_id, message_id)
        updated, deleted = await _collect(stream, 2)
        await stream.aclose()
        assert added.kind == "message_added"
        assert updated.kind == "message_updated" and updated.message
        assert updated.message.text == "v2"
        assert deleted.kind == "message_deleted" and deleted.message
        assert deleted.message.id == message_id

    async def test_a_server_drop_ends_the_stream_without_hanging(
        self, mock: MockAnytype, chat: AnytypeChatClient
    ) -> None:
        chat_id = mock.seed_chat()
        stream = chat.stream(chat_id)
        first = asyncio.ensure_future(anext(stream))
        await asyncio.sleep(0.01)
        mock.end_chat_streams(chat_id)
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(first, 5)

    async def test_malformed_frames_are_dropped_not_fatal(self) -> None:
        async def lines() -> AsyncIterator[str]:
            for line in (
                "event: message_added",
                "data: {this is not json",
                "",
                "event: message_added",
                'data: {"type": "message_added", "payload": {"message": '
                '{"id": "m1", "creator": "c", "order_id": "o1", '
                '"content": {"text": "survives"}}}}',
                "",
            ):
                yield line

        events = [event async for event in parse_sse(lines())]
        assert [e.message.text for e in events if e.message] == ["survives"]


class TestIdentityDiscovery:
    async def test_a_solo_member_space_names_the_bot_identity(
        self, mock: MockAnytype, client: AnytypeClient
    ) -> None:
        from graph_context.infrastructure.anytype.chat import discover_bot_identity

        mock.members = [{
            "object": "member",
            "id": f"_participant_{mock.space_id}_A73Wbot",
            "identity": "A73Wbot", "name": "graph-context-bot",
            "status": "active", "role": "owner",
        }]
        assert await discover_bot_identity(client) == "A73Wbot"

    async def test_a_shared_space_yields_no_identity(
        self, mock: MockAnytype, client: AnytypeClient
    ) -> None:
        from graph_context.infrastructure.anytype.chat import discover_bot_identity

        mock.members = [
            {"object": "member", "id": "m1", "identity": "A73Wbot",
             "status": "active", "role": "editor"},
            {"object": "member", "id": "m2", "identity": "AA5Khuman",
             "status": "active", "role": "owner"},
        ]
        assert await discover_bot_identity(client) == ""


class TestChatDiscovery:
    async def test_lists_every_chat_as_id_name_pairs(
        self, mock: MockAnytype, chat: AnytypeChatClient
    ) -> None:
        a = mock.seed_chat("Plot")
        b = mock.seed_chat("Characters")
        assert set(await chat.list_chats()) == {(a, "Plot"), (b, "Characters")}

    async def test_no_chats_lists_empty(self, chat: AnytypeChatClient) -> None:
        assert await chat.list_chats() == []


class TestFiles:
    """WP23 (quirk C10): upload/download through the chat client, and the
    attachment surfaces the transport builds on."""

    async def test_upload_download_round_trip(
        self, chat: AnytypeChatClient
    ) -> None:
        file_id = await chat.upload_file("notes.txt", b"hello file world")
        content, media = await chat.fetch_file(file_id)
        assert content == b"hello file world"
        assert media.startswith("text/plain")

    async def test_attachment_facts_classify_ready(
        self, chat: AnytypeChatClient
    ) -> None:
        file_id = await chat.upload_file("data.csv", b"a,b\n1,2\n")
        facts = await chat.attachment_facts(file_id)
        assert facts == {
            "name": "data", "type_key": "file",
            "size_in_bytes": 8, "extension": "csv",
        }
        image_id = await chat.upload_file("pic.png", b"\x89PNGfake")
        assert (await chat.attachment_facts(image_id))["type_key"] == "image"

    async def test_send_file_message_attaches_a_file_envelope(
        self, mock: MockAnytype, chat: AnytypeChatClient
    ) -> None:
        chat_id = mock.seed_chat()
        file_id = await chat.upload_file("report.md", b"# Report")
        await chat.send_file_message(chat_id, "\N{PAPERCLIP} report.md", file_id)
        (stored,) = mock.chat_messages(chat_id)
        assert stored["attachments"] == [{"target": file_id, "type": "file"}]

    async def test_inbound_attachments_are_parsed(
        self, mock: MockAnytype, chat: AnytypeChatClient
    ) -> None:
        chat_id = mock.seed_chat()
        file_id = mock.seed_file("photo", b"png-bytes", media="image/png",
                                 extension="png")
        mock.post_chat_message_directly(
            chat_id, "human", "look at this",
            attachments=[{"target": file_id, "type": "file"}],
        )
        (message,) = await chat.recent_messages(chat_id)
        (attachment,) = message.attachments
        assert attachment.target == file_id
        assert attachment.type == "file"


class TestReactions:
    """C12 (spike S15): the toggle route, the populated reactions shape,
    and the envelope-free reactions_updated SSE frame."""

    async def test_toggle_round_trips_and_second_toggle_removes(
        self, mock: MockAnytype, chat: AnytypeChatClient
    ) -> None:
        chat_id = mock.seed_chat()
        message_id = await chat.send(chat_id, "confirm me")
        await chat.toggle_reaction(chat_id, message_id, "\N{THUMBS UP SIGN}")
        (message,) = await chat.recent_messages(chat_id)
        assert message.reactions == {
            "\N{THUMBS UP SIGN}": (mock.api_identity,)
        }
        await chat.toggle_reaction(chat_id, message_id, "\N{THUMBS UP SIGN}")
        (message,) = await chat.recent_messages(chat_id)
        assert message.reactions == {}

    async def test_reactions_updated_frame_carries_id_and_map_bare(
        self, mock: MockAnytype, chat: AnytypeChatClient
    ) -> None:
        chat_id = mock.seed_chat()
        message_id = mock.post_chat_message_directly(chat_id, "human", "hi")
        stream = chat.stream(chat_id)
        await _collect(stream, 1)  # fast-forward the backlog replay
        mock.react_directly(
            chat_id, message_id, "\N{THUMBS UP SIGN}", "human-identity"
        )
        (event,) = await _collect(stream, 1)
        assert event.kind == "reactions_updated"
        assert event.message is None  # C12: no message envelope
        assert event.message_id == message_id
        assert event.reactions == {
            "\N{THUMBS UP SIGN}": ("human-identity",)
        }

    async def test_a_human_and_the_bot_can_share_an_emoji(
        self, mock: MockAnytype, chat: AnytypeChatClient
    ) -> None:
        chat_id = mock.seed_chat()
        message_id = await chat.send(chat_id, "confirm me")
        mock.react_directly(
            chat_id, message_id, "\N{THUMBS UP SIGN}", "human-identity"
        )
        await chat.toggle_reaction(chat_id, message_id, "\N{THUMBS UP SIGN}")
        (message,) = await chat.recent_messages(chat_id)
        assert set(message.reactions["\N{THUMBS UP SIGN}"]) == {
            "human-identity", mock.api_identity,
        }
