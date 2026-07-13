"""Bootstrap pins for the Scheduled Event surface (WP18, ADR 027).

Humans create these objects directly in the Anytype editor, so the
bootstrap must hand them a usable surface: properties with human display
names (keys stay ``gc_`` for wire stability) and a seeded explainer
object that can never fire.
"""

from graph_context.infrastructure.anytype import mapping
from graph_context.infrastructure.anytype.schema_bootstrap import (
    EXAMPLE_EVENT_NAME,
    SCHEDULED_TYPE_KEY,
    ensure_schema,
)


class TestScheduledEventBootstrap:
    async def test_properties_mint_with_human_display_names(
        self, mock, client, repo
    ) -> None:
        names = {p["key"]: p["name"] async for p in client.list_properties()}
        assert names[mapping.PROP_SCHEDULE] == "Schedule"
        assert names[mapping.PROP_SCHEDULE_PROMPT] == "Schedule prompt"
        assert names[mapping.PROP_SCHEDULE_STATUS] == "Schedule status"
        assert names[mapping.PROP_LAST_FIRED] == "Last fired"
        assert names[mapping.PROP_SESSION_KEY] == "Session key"

    async def test_the_status_property_is_a_select(
        self, mock, client, repo
    ) -> None:
        formats = {p["key"]: p["format"] async for p in client.list_properties()}
        assert formats[mapping.PROP_SCHEDULE_STATUS] == "select"

    async def test_the_example_event_is_seeded_and_can_never_fire(
        self, mock, client, repo
    ) -> None:
        example = next(
            n for n in repo.graph.nodes()
            if n.type_key == SCHEDULED_TYPE_KEY and n.name == EXAMPLE_EVENT_NAME
        )
        # An empty schedule is the fire-safety: is_disabled skips it.
        assert not example.fields.get(mapping.PROP_SCHEDULE, "")
        assert example.fields.get(mapping.PROP_SCHEDULE_PROMPT)
        body = await repo.fetch_body(example.id)
        assert "Schedule status" in body  # the in-space documentation

    async def test_rerunning_bootstrap_does_not_duplicate_the_example(
        self, mock, client, repo
    ) -> None:
        await ensure_schema(client)  # second run: type exists, no re-seed
        await repo.resync()
        examples = [
            n for n in repo.graph.nodes()
            if n.type_key == SCHEDULED_TYPE_KEY and n.name == EXAMPLE_EVENT_NAME
        ]
        assert len(examples) == 1
