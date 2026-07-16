"""Live chat round-trip (WP14): the mock's fidelity contract for chat.

Pins against a real server the exact behaviors the mock asserts in
``tests/anytype/test_chat_client.py``: chat creation via API (S10e), the
flat ``message_id`` create response (C1), the ``messages`` recency window
(C2), SSE ``message_added`` framing with heartbeat comments (C5), the
wholesale-replacement edit (C8), the object-route rename (C9), and
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

            # C8: an edit replaces content wholesale -- attachments in the
            # body land; attachments absent from a later edit are removed.
            card = await client.create_object(
                {"name": "E2E edit card", "type_key": "page"}
            )
            await chat_client.edit(
                chat_id, message_id, "e2e ping v2",
                attachments=(str(card["id"]),),
            )
            (edited,) = await client.list_chat_messages(chat_id)
            assert edited["content"]["text"] == "e2e ping v2"
            assert edited["attachments"] == [
                {"target": str(card["id"]), "type": "link"}
            ]
            await chat_client.edit(chat_id, message_id, "e2e ping v3")
            (edited,) = await client.list_chat_messages(chat_id)
            assert edited["content"]["text"] == "e2e ping v3"
            assert not edited.get("attachments")  # C8: wiped, not kept

            await client.delete_chat_message(chat_id, message_id)
            assert await client.list_chat_messages(chat_id) == []

            # C9 (spike S12): the rename rides the generic object PATCH
            # (the /chats namespace has no update route) and the /chats
            # re-list reflects it.
            await chat_client.rename(chat_id, "E2E Chat renamed")
            names = dict(await chat_client.list_chats())
            assert names[chat_id] == "E2E Chat renamed"

            # C10 (spike S13): upload -> attach -> inbound exposure ->
            # download, byte-faithful.
            file_id = await chat_client.upload_file(
                "e2e-notes.txt", b"file round trip"
            )
            facts = await chat_client.attachment_facts(file_id)
            assert facts["type_key"] == "file"
            assert facts["extension"] == "txt"
            assert facts["size_in_bytes"] == 15
            posted = await chat_client.send_file_message(
                chat_id, "\N{PAPERCLIP} e2e-notes.txt", file_id
            )
            assert posted
            window = await chat_client.recent_messages(chat_id)
            carrying = next(m for m in window if m.id == posted)
            assert any(a.target == file_id for a in carrying.attachments)
            content, media = await chat_client.fetch_file(file_id)
            assert content == b"file round trip"
            assert media.startswith("text/plain")
        finally:
            await client.aclose()
