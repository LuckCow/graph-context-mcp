"""Live chat round-trip (WP14): the mock's fidelity contract for chat.

Pins against a real server the exact behaviors the mock asserts in
``tests/anytype/test_chat_client.py``: chat creation via API (S10e), the
flat ``message_id`` create response (C1), the ``messages`` recency window
(C2), SSE ``message_added`` framing with heartbeat comments (C5), and
message deletion. Gated by ``ANYTYPE_E2E=1`` like the rest of the suite.

NOTE: the session-scoped ``live_config`` resets the GC-E2E space before
and after the run -- spike artifacts (S9/S10 chats, sets, todos) do not
survive an E2E run; the spike scripts reseed themselves.
"""

from __future__ import annotations

import asyncio

from graph_context.infrastructure.anytype.chat import AnytypeChatClient
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.config import AnytypeConfig


class TestLiveChat:
    async def test_post_stream_edit_delete_round_trip(
        self, live_config: AnytypeConfig
    ) -> None:
        client = AnytypeClient(live_config)
        try:
            chat_client = AnytypeChatClient(client)
            created = await client.create_chat({"name": "E2E Chat"})
            chat_id = str(created["id"])

            # The chat is enumerable by list_chats (WP8 serve-all).
            assert chat_id in {cid for cid, _ in await chat_client.list_chats()}

            stream = chat_client.stream(chat_id, heartbeat_seconds=5)
            first = asyncio.ensure_future(anext(stream))
            await asyncio.sleep(1.0)  # stream connected (empty backlog)
            message_id = await chat_client.send(chat_id, "e2e ping")
            assert message_id  # C1: flat message_id from the 201

            event = await asyncio.wait_for(first, 30)
            while event.kind != "message_added":  # heartbeats may interleave
                event = await asyncio.wait_for(anext(stream), 30)
            assert event.message is not None
            assert event.message.text == "e2e ping"
            assert event.message.id == message_id
            await stream.aclose()

            window = await client.list_chat_messages(chat_id)  # C2
            assert [m["id"] for m in window] == [message_id]

            await client.delete_chat_message(chat_id, message_id)
            assert await client.list_chat_messages(chat_id) == []
        finally:
            await client.aclose()
