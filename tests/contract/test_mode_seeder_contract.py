"""Starter-mode seeding (ADR 035): the heal, pinned against the mock.

The seeder converts a mode-less space into a working one exactly once:
mints one Activity Mode object per seed payload plus the Example Mode
explainer, and links the marked default on the Space Context -- then
never touches the space again. The crown jewel is the round trip: a
seeded space, read back through ``AnytypeModeStore`` and
``load_registry``, must equal the registry built directly from the same
payloads (the memory backend's path), so both backends serve identical
modes from one corpus.
"""

from __future__ import annotations

import pytest

from graph_context.infrastructure.anytype import mapping
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.config import AnytypeConfig
from graph_context.infrastructure.anytype.mock_server import MockAnytype
from graph_context.infrastructure.anytype.mode_seeder import (
    EXAMPLE_MODE_NAME,
    seed_activity_modes,
)
from graph_context.infrastructure.anytype.mode_store import AnytypeModeStore
from graph_context.infrastructure.anytype.schema_bootstrap import (
    MODE_TYPE_KEY,
    SPACE_CONTEXT_TYPE_KEY,
    ensure_schema,
)
from graph_context.infrastructure.anytype.space_context_store import (
    AnytypeSpaceContextStore,
)
from graph_context.interface.mode_config import load_seed_modes, seed_payloads
from graph_context.orchestrator.modes import load_registry

FICTION_PAYLOADS = seed_payloads(load_seed_modes(None, "fiction"))
ASSISTANT_PAYLOADS = seed_payloads(load_seed_modes(None, "assistant"))


@pytest.fixture
def mock() -> MockAnytype:
    return MockAnytype()


@pytest.fixture
async def anytype_client(mock: MockAnytype) -> AnytypeClient:
    client = AnytypeClient(
        AnytypeConfig(api_key="t", space_id=mock.space_id), transport=mock.transport
    )
    await ensure_schema(client)  # types + the Space Context singleton
    return client


async def _loaded_registry(client: AnytypeClient):
    return load_registry(
        in_space=await AnytypeModeStore(client).load(),
        space_context=await AnytypeSpaceContextStore(client).load(),
    )


async def test_a_fresh_space_is_seeded_with_the_corpus_and_explainer(
    anytype_client: AnytypeClient,
) -> None:
    assert await seed_activity_modes(anytype_client, FICTION_PAYLOADS) is True
    payloads = await AnytypeModeStore(anytype_client).load()
    names = {p["name"] for p in payloads}
    assert names == {"World Modeling", "Authoring", EXAMPLE_MODE_NAME}
    authoring = next(p for p in payloads if p["name"] == "Authoring")
    assert authoring["capture"] == {
        "artifact_type": "gc_prose",
        "references_label": "references",
        "min_chars": 200,
    }
    assert not authoring["mutating"]


async def test_the_marked_default_is_linked_on_the_space_context(
    anytype_client: AnytypeClient,
) -> None:
    await seed_activity_modes(anytype_client, ASSISTANT_PAYLOADS)
    registry = await _loaded_registry(anytype_client)
    assert registry.default == "organizing"  # marked, NOT alphabetical


async def test_round_trip_matches_the_directly_built_registry(
    anytype_client: AnytypeClient,
) -> None:
    """Memory path == Anytype path: one corpus, identical registries.

    (The seeded space also carries the explainer as example_mode -- the
    corpus specs themselves must round-trip exactly.)"""
    await seed_activity_modes(anytype_client, FICTION_PAYLOADS)
    seeded = await _loaded_registry(anytype_client)
    direct = load_registry(in_space=FICTION_PAYLOADS, space_context=[{
        "name": "Space Context", "origin": "test",
        "default_mode_ids": ["seed:world_modeling"],
    }])
    for name, spec in direct.specs.items():
        assert seeded.specs[name] == spec
    assert seeded.default == direct.default


async def test_a_second_run_is_a_no_op(anytype_client: AnytypeClient) -> None:
    await seed_activity_modes(anytype_client, FICTION_PAYLOADS)
    before = {p["id"] for p in await AnytypeModeStore(anytype_client).load()}
    assert await seed_activity_modes(anytype_client, FICTION_PAYLOADS) is False
    after = {p["id"] for p in await AnytypeModeStore(anytype_client).load()}
    assert after == before  # no duplicates


async def test_a_space_with_any_mode_object_is_never_touched(
    anytype_client: AnytypeClient, mock: MockAnytype
) -> None:
    """One human-authored (or leftover) mode object means the space is
    in-space-configured; the seeder must not add starters beside it."""
    mock.seed_object(MODE_TYPE_KEY, "My Mode", [], body="A goal.")
    assert await seed_activity_modes(anytype_client, FICTION_PAYLOADS) is False
    payloads = await AnytypeModeStore(anytype_client).load()
    assert [p["name"] for p in payloads] == ["My Mode"]


async def test_an_existing_default_link_is_never_clobbered(
    anytype_client: AnytypeClient, mock: MockAnytype
) -> None:
    """A pre-linked Space Context is a human's choice: the seeder still
    mints the starters (the space had no modes) but leaves the link."""
    context = await anext(
        anytype_client.search(types=[SPACE_CONTEXT_TYPE_KEY])
    )
    await anytype_client.update_object(context["id"], {"properties": [
        mapping.property_entry(
            mapping.PROP_DEFAULT_MODE, "objects", ["human-choice"]
        ),
    ]})
    await seed_activity_modes(anytype_client, FICTION_PAYLOADS)
    (payload,) = await AnytypeSpaceContextStore(anytype_client).load()
    assert payload["default_mode_ids"] == ["human-choice"]


async def test_a_missing_space_context_degrades_to_a_warning(
    mock: MockAnytype, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No Space Context object (human deleted it): the starters still
    seed; the default falls back alphabetically at load."""
    from graph_context.infrastructure.anytype import mode_seeder

    # The bounded not-found poll is a live-server settle allowance; the
    # mock answers instantly, so waiting 20x here is pure test latency.
    monkeypatch.setattr(mode_seeder, "_SETTLE_DELAY_SECONDS", 0.0)
    client = AnytypeClient(
        AnytypeConfig(api_key="t", space_id=mock.space_id),
        transport=mock.transport,
    )
    # Minimal schema by hand: the mode type only, NO space context.
    await client.create_type({
        "key": MODE_TYPE_KEY, "name": "Activity Mode",
        "plural_name": "Activity Modes", "layout": "basic",
        "properties": [
            {"key": key, "name": key, "format": fmt}
            for key, fmt in mapping.MODE_PROPERTIES.items()
        ],
    })
    assert await seed_activity_modes(client, FICTION_PAYLOADS) is True
    registry = load_registry(in_space=await AnytypeModeStore(client).load())
    assert registry.default == "authoring"  # alphabetical backstop
    await client.aclose()
