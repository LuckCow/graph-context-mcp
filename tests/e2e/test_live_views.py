"""Live view catalog (WP13): skip-behaviors against a real server.

The GC-E2E space starts reset, so the catalog is exercised on its skip
paths: an API-created set is SOURCELESS (S9) and must be skipped without
failing the load. The positive compile path is covered mock-side in
``tests/anytype/test_view_catalog.py`` (a configured source cannot be
created programmatically -- desktop-only, quirk V1).
"""

from __future__ import annotations

from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.config import AnytypeConfig
from graph_context.infrastructure.anytype.view_catalog import AnytypeViewCatalog


class TestLiveViewCatalog:
    async def test_sourceless_sets_are_skipped_not_fatal(
        self, live_config: AnytypeConfig
    ) -> None:
        client = AnytypeClient(live_config)
        try:
            await client.create_object({"type_key": "set", "name": "E2E Shell Set"})
            views = await AnytypeViewCatalog(client).load()
            assert isinstance(views, tuple)
            assert all(v.set_name != "E2E Shell Set" for v in views)
        finally:
            await client.aclose()
