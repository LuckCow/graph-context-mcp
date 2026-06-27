"""Live hydrate/resync behavior the contract can't express.

Validates the spike-driven sync design against a real server: search-based
modified-since resync, self-write suppression, and the S4 deletion blind
spot reconciled only by a full hydrate.
"""

from __future__ import annotations

import asyncio

from graph_context.domain.models import NodeDraft

PROBE = NodeDraft("Character", name="ResyncProbe", summary="A probe.")


async def test_resync_picks_up_out_of_band_edit(repo, raw_api):
    node = await repo.create_node(PROBE)
    # Our own write must not be reported as an out-of-band change.
    assert await repo.resync() == frozenset()
    # Let the human edit land in a later second than our write -- last_modified_date
    # is second-granular (spike S3), so a same-second edit is indistinguishable.
    await asyncio.sleep(1.5)
    raw_api.rename(node.id, "Renamed Probe")
    changed = await repo.resync()
    assert node.id in changed
    assert repo.graph.node(node.id).name == "Renamed Probe"


async def test_deletion_invisible_to_resync_but_hydrate_reconciles(repo, raw_api):
    node = await repo.create_node(
        NodeDraft("Location", name="DoomedPlace", summary="To be archived.")
    )
    raw_api.archive(node.id)
    # Archived objects are invisible to list and search (spike S4), so the
    # incremental resync cannot see the deletion...
    assert await repo.resync() == frozenset()
    assert repo.graph.has_node(node.id)
    # ...only a full hydrate, which rebuilds from the live set, drops it.
    await repo.hydrate()
    assert not repo.graph.has_node(node.id)
