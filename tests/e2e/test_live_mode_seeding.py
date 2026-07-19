"""Live starter-mode seeding (ADR 035): the heal against the real server.

The suite reset leaves GC-E2E with no Activity Mode objects, which is
exactly the state the seeder heals. This pins the parts the mock cannot:
the fresh-object search settle (the seeder's bounded verify-poll) and
the real relation write for the default link. The reset also archives
any bootstrap-seeded Space Context, so the test provisions one first --
the same shape a fresh space gets at type mint.
"""

from __future__ import annotations

from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.mode_seeder import seed_activity_modes
from graph_context.infrastructure.anytype.mode_store import AnytypeModeStore
from graph_context.infrastructure.anytype.schema_bootstrap import (
    SPACE_CONTEXT_TYPE_KEY,
)
from graph_context.infrastructure.anytype.space_context_store import (
    AnytypeSpaceContextStore,
)
from graph_context.interface.mode_config import load_seed_modes, seed_payloads
from graph_context.orchestrator.modes import load_registry


async def test_seeding_heals_a_modeless_space_once(live_config):
    client = AnytypeClient(live_config)
    try:
        contexts = await AnytypeSpaceContextStore(client).load()
        if not contexts:  # the reset archived the seeded singleton
            await client.create_object({
                "name": "Space Context",
                "type_key": SPACE_CONTEXT_TYPE_KEY,
            })
        payloads = seed_payloads(load_seed_modes(None, "fiction"))
        assert await seed_activity_modes(client, payloads) is True
        registry = load_registry(
            in_space=await AnytypeModeStore(client).load(),
            space_context=await AnytypeSpaceContextStore(client).load(),
        )
        assert {"world_modeling", "authoring", "example_mode"} <= set(
            registry.specs
        )
        assert registry.default == "world_modeling"  # the seeded link
        authoring = registry.specs["authoring"]
        assert authoring.capture is not None
        assert authoring.capture.artifact_type == "gc_prose"
        # The heal is once: a second run must not duplicate anything.
        assert await seed_activity_modes(client, payloads) is False
    finally:
        await client.aclose()
