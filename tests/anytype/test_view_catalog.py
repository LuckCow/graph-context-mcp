"""View catalog: S9-shaped view definitions compile to NodeQuery (WP13).

Pins quirks V1-V5 from ``view_catalog.py`` against the mock's lists
routes; the live-gated ``tests/e2e/test_live_views.py`` replays the
skip-behaviors against a real server.
"""

from __future__ import annotations

from graph_context.domain.query import NodeQuery, Op, Predicate, SortKey
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.mock_server import MockAnytype
from graph_context.infrastructure.anytype.view_catalog import AnytypeViewCatalog


# The exact shape a configured view came back with live (S9), including
# the useless format="text" on sorts and the stale byLastModifiedDate id.
def _open_tasks_view(priority_prop_id: str) -> dict:
    return {
        "id": "default",
        "name": "All",
        "layout": "grid",
        "filters": [
            {"id": "f1", "property_key": "done", "format": "checkbox",
             "condition": "eq", "value": ""},
        ],
        "sorts": [
            {"id": "byLastModifiedDate", "property_key": "dueDate",
             "format": "text", "sort_type": "asc"},
            {"id": "s2", "property_key": priority_prop_id,
             "format": "text", "sort_type": "desc"},
        ],
    }


class TestViewCompilation:
    async def test_a_configured_view_compiles_to_a_node_query(
        self, mock: MockAnytype, client: AnytypeClient
    ) -> None:
        priority = await client.create_property(
            {"key": "priority", "name": "Priority", "format": "select"}
        )
        mock.seed_object("task", "Buy milk")  # inference sample (V4)
        mock.seed_set(
            "Open Tasks", source_type_key="task",
            views=[_open_tasks_view(priority["id"])],
        )
        (view,) = await AnytypeViewCatalog(client).load()
        assert view.set_name == "Open Tasks" and view.view_name == "All"
        assert view.full_name == "Open Tasks/All"
        assert view.query == NodeQuery(
            node_type="task",
            # V3: checkbox eq "" is un-checked-ness = the absence idiom.
            predicates=(Predicate("done", Op.NEQ, "true"),),
            # V2: camelCase built-in and internal property id both
            # translate to real keys.
            order_by=(SortKey("due_date"), SortKey("priority", descending=True)),
        )

    async def test_an_unknown_condition_skips_the_view_not_the_catalog(
        self, mock: MockAnytype, client: AnytypeClient
    ) -> None:
        mock.seed_object("task", "Buy milk")
        mock.seed_set("Weird", source_type_key="task", views=[{
            "id": "v1", "name": "All",
            "filters": [{"property_key": "done", "format": "checkbox",
                         "condition": "quantum_entangled", "value": ""}],
            "sorts": [],
        }])
        mock.seed_set("Fine", source_type_key="task", views=[{
            "id": "v1", "name": "All", "filters": [], "sorts": [],
        }])
        views = await AnytypeViewCatalog(client).load()
        assert [v.set_name for v in views] == ["Fine"]

    async def test_a_sourceless_set_is_skipped_like_live(
        self, mock: MockAnytype, client: AnytypeClient
    ) -> None:
        # S9: view execution on an API-created (sourceless) set 500s.
        mock.seed_set("Shell", source_type_key=None, views=[{
            "id": "v1", "name": "All", "filters": [], "sorts": [],
        }])
        assert await AnytypeViewCatalog(client).load() == ()

    async def test_an_empty_view_is_skipped_because_type_inference_fails(
        self, mock: MockAnytype, client: AnytypeClient
    ) -> None:
        # V4: no objects -> no source type -> a typeless absence-matching
        # query would match the whole world; skip instead.
        mock.seed_set("Empty", source_type_key="task", views=[{
            "id": "v1", "name": "All", "filters": [], "sorts": [],
        }])
        assert await AnytypeViewCatalog(client).load() == ()

    async def test_list_filter_values_are_not_mangled_but_skipped(
        self, mock: MockAnytype, client: AnytypeClient
    ) -> None:
        mock.seed_object("task", "Buy milk")
        mock.seed_set("Tagged", source_type_key="task", views=[{
            "id": "v1", "name": "All",
            "filters": [{"property_key": "tag", "format": "multi_select",
                         "condition": "in", "value": ["a", "b"]}],
            "sorts": [],
        }])
        assert await AnytypeViewCatalog(client).load() == ()

    async def test_an_unresolvable_internal_sort_key_is_dropped_not_fatal(
        self, mock: MockAnytype, client: AnytypeClient
    ) -> None:
        """V6 (live-caught): views leak internal 24-hex relation keys for
        API-created properties; REST cannot resolve them. A sort degrades
        by one tiebreaker; the view still runs."""
        mock.seed_object("task", "Buy milk")
        mock.seed_set("Open Tasks", source_type_key="task", views=[{
            "id": "v1", "name": "All", "filters": [],
            "sorts": [
                {"property_key": "dueDate", "format": "text", "sort_type": "asc"},
                {"property_key": "6a4db8938f92ca000146b420",
                 "format": "text", "sort_type": "desc"},
            ],
        }])
        (view,) = await AnytypeViewCatalog(client).load()
        assert view.query.order_by == (SortKey("due_date"),)

    async def test_an_unresolvable_internal_filter_key_skips_the_view(
        self, mock: MockAnytype, client: AnytypeClient
    ) -> None:
        mock.seed_object("task", "Buy milk")
        mock.seed_set("Broken", source_type_key="task", views=[{
            "id": "v1", "name": "All",
            "filters": [{"property_key": "6a4db8938f92ca000146b420",
                         "format": "select", "condition": "eq", "value": "x"}],
            "sorts": [],
        }])
        assert await AnytypeViewCatalog(client).load() == ()

    async def test_a_desktop_created_hex_key_that_exists_is_kept(
        self, mock: MockAnytype, client: AnytypeClient
    ) -> None:
        # V6's boundary: desktop-created properties legitimately HAVE hex
        # keys -- resolvable ones must not be dropped.
        await client.create_property(
            {"key": "67dab1b02bf7cb67453ef126", "name": "Date", "format": "date"}
        )
        mock.seed_object("task", "Buy milk")
        mock.seed_set("Dated", source_type_key="task", views=[{
            "id": "v1", "name": "All", "filters": [],
            "sorts": [{"property_key": "67dab1b02bf7cb67453ef126",
                       "format": "text", "sort_type": "asc"}],
        }])
        (view,) = await AnytypeViewCatalog(client).load()
        assert view.query.order_by == (SortKey("67dab1b02bf7cb67453ef126"),)
