"""Chat client behavior against the mock server (WP14, quirks C1-C5).

The mock pins the live shapes found by spike S10; the live-gated
``tests/e2e/test_live_chat.py`` asserts the same behaviors against a real
server, so these tests double as the mock's fidelity contract.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from graph_context.errors import GraphContextError
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
    async def test_a_declared_chat_id_passes_through(
        self, chat: AnytypeChatClient
    ) -> None:
        assert await chat.resolve_chat_id("chat-xyz") == "chat-xyz"

    async def test_an_undeclared_chat_id_resolves_when_the_space_has_exactly_one_chat(
        self, mock: MockAnytype, chat: AnytypeChatClient
    ) -> None:
        chat_id = mock.seed_chat("General")
        assert await chat.resolve_chat_id(None) == chat_id

    async def test_zero_or_many_chats_fail_loudly_naming_the_space(
        self, mock: MockAnytype, chat: AnytypeChatClient
    ) -> None:
        with pytest.raises(GraphContextError, match="no chat"):
            await chat.resolve_chat_id(None)
        mock.seed_chat("A")
        mock.seed_chat("B")
        with pytest.raises(GraphContextError, match="chat_id"):
            await chat.resolve_chat_id(None)
