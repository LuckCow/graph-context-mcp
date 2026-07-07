"""WP14 composition-root wiring: startup failure modes + the serve loop.

Uses the MockAnytype chat routes end-to-end for the transport side and an
in-memory runtime (scripted driver) for the turn side -- the same split
the live bot has: transport clients and repository clients are separate.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

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
from graph_context.orchestrator.anytype_chat_bot import _catch_up, _serve_chat
from graph_context.orchestrator.anytype_chat_transport import (
    AnytypeChatTurnHandler,
    ChatCursor,
)
from graph_context.orchestrator.channels import ChannelRoute
from graph_context.orchestrator.drivers import LLMTurn, ScriptedDriver
from graph_context.orchestrator.modes import load_registry
from graph_context.orchestrator.pipeline import Orchestrator

FICTION = get_profile("fiction")


async def _noop_resolver(binding: object) -> str:
    return "unused"


class TestStartup:
    async def test_startup_fails_loudly_when_gc_spaces_file_is_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GC_SPACES_FILE", raising=False)
        with pytest.raises(GraphContextError, match="GC_SPACES_FILE"):
            await bootstrap.build_space_runtimes(_noop_resolver)  # type: ignore[arg-type]


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
            async with asyncio.timeout(5):
                while True:
                    texts = [
                        m["content"]["text"]
                        for m in mock._chat_messages[chat_id]
                        if m["creator"] == mock.api_member_id
                    ]
                    if texts:
                        break
                    await asyncio.sleep(0.01)
            assert texts == ["hello from the graph"]
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
