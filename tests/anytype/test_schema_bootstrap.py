"""Bootstrap pins for the Scheduled Event surface (WP18, ADR 027).

Humans create these objects directly in the Anytype editor, so the
bootstrap must hand them a usable surface: properties with human display
names (keys stay ``gc_`` for wire stability) and a seeded explainer
object that can never fire.
"""

from graph_context.domain import rules
from graph_context.infrastructure.anytype import mapping
from graph_context.infrastructure.anytype.schema_bootstrap import (
    EXAMPLE_EVENT_NAME,
    EXAMPLE_RULE_NAME,
    RULE_TYPE_KEY,
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
        assert names[mapping.PROP_SCHEDULE_MESSAGE] == "Schedule message"
        assert names[mapping.PROP_SCHEDULE_MODE] == "Schedule mode"
        assert (
            names[mapping.PROP_SCHEDULE_DOCUMENT_TYPE]
            == "Schedule document type"
        )
        assert names[mapping.PROP_SCHEDULE_STATUS] == "Schedule status"
        assert names[mapping.PROP_LAST_FIRED] == "Last fired"
        assert names[mapping.PROP_SESSION_KEY] == "Session key"

    async def test_the_status_property_is_a_select(
        self, mock, client, repo
    ) -> None:
        formats = {p["key"]: p["format"] async for p in client.list_properties()}
        assert formats[mapping.PROP_SCHEDULE_STATUS] == "select"
        # ADR 055: mode is deliberately TEXT, not select -- mode names
        # are live space data; pre-seeded options would go stale.
        assert formats[mapping.PROP_SCHEDULE_MODE] == "text"
        assert formats[mapping.PROP_SCHEDULE_MESSAGE] == "text"

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
        # ADR 055: the explainer teaches the one-of choice and the mode.
        assert "Schedule message" in body and "Schedule mode" in body
        assert "the message wins" in body
        # ADR 057: and the per-schedule document output option.
        assert "Schedule document type" in body

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


class TestAutomationRuleBootstrap:
    """The WP31/ADR 039 surface: same human-usability pins as above."""

    async def test_properties_mint_with_human_display_names(
        self, mock, client, repo
    ) -> None:
        names = {p["key"]: p["name"] async for p in client.list_properties()}
        for key, display in {
            mapping.PROP_RULE_TARGET_TYPE: "Rule target type",
            mapping.PROP_RULE_WATCH_PROPERTY: "Rule watch property",
            mapping.PROP_RULE_CONDITION: "Rule condition",
            mapping.PROP_RULE_ACTION: "Rule action",
            mapping.PROP_RULE_ACTION_PROPERTY: "Rule action property",
            mapping.PROP_RULE_ACTION_VALUE: "Rule action value",
            mapping.PROP_RULE_STATUS: "Rule status",
            mapping.PROP_RULE_LAST_FIRED: "Rule last fired",
            mapping.PROP_RULE_LAST_ERROR: "Rule last error",
            mapping.PROP_RULE_RUN_NOW: "Rule run now",
        }.items():
            assert names[key] == display

    async def test_run_now_mints_as_a_checkbox(self, mock, client, repo) -> None:
        # WP52 (ADR 058): the human's manual-run gesture is a tickable
        # box in the editor, so the format is load-bearing.
        by_key = {p["key"]: p async for p in client.list_properties()}
        assert by_key[mapping.PROP_RULE_RUN_NOW]["format"] == "checkbox"

    async def test_the_vocabulary_selects_have_seeded_options(
        self, mock, client, repo
    ) -> None:
        by_key = {p["key"]: p async for p in client.list_properties()}
        for key, expected in {
            mapping.PROP_RULE_CONDITION: {
                "Changed to true", "Changed to false", "Changed", "Manual",
            },
            mapping.PROP_RULE_ACTION: {
                "Set property to now", "Set property value", "Uncheck others of type",
            },
            mapping.PROP_RULE_STATUS: {"Active", "Paused", "Error"},
        }.items():
            assert by_key[key]["format"] == "select"
            options = {
                t["name"] async for t in client.list_tags(str(by_key[key]["id"]))
            }
            assert expected <= options

    async def test_the_example_rule_is_seeded_and_can_never_run(
        self, mock, client, repo
    ) -> None:
        example = next(
            n for n in repo.graph.nodes()
            if n.type_key == RULE_TYPE_KEY and n.name == EXAMPLE_RULE_NAME
        )
        # An empty config is the run-safety: is_unconfigured skips it.
        assert rules.is_unconfigured(example.fields)
        body = await repo.fetch_body(example.id)
        assert "Rule target type" in body  # the in-space documentation
        assert "Uncheck others of type" in body
        assert "Rule run now" in body  # ADR 058's manual-run gesture
        assert "/run <rule name>" in body

    async def test_rerunning_bootstrap_does_not_duplicate_the_example(
        self, mock, client, repo
    ) -> None:
        await ensure_schema(client)
        await repo.resync()
        examples = [
            n for n in repo.graph.nodes()
            if n.type_key == RULE_TYPE_KEY and n.name == EXAMPLE_RULE_NAME
        ]
        assert len(examples) == 1


class TestRunNowRetrofit:
    """WP52 (ADR 058): the upgraded-space story. A space bootstrapped
    before Rule run now existed gains it on the next startup -- no
    migration, because ensure_schema retrofits missing properties onto
    an existing type (quirk A11: full list resent + the additions)."""

    async def test_an_existing_rule_type_gains_run_now(
        self, mock, client, repo
    ) -> None:
        pre_wp52 = {
            key: fmt for key, fmt in mapping.RULE_PROPERTIES.items()
            if key != mapping.PROP_RULE_RUN_NOW
        }
        rule_type = {t["key"]: t async for t in client.list_types()}[RULE_TYPE_KEY]
        await client.update_type(str(rule_type["id"]), {
            "properties": [
                {"key": key, "name": key, "format": fmt}
                for key, fmt in pre_wp52.items()
            ],
        })

        await ensure_schema(client)

        retrofitted = {t["key"]: t async for t in client.list_types()}[RULE_TYPE_KEY]
        attached = {e["key"] for e in retrofitted.get("properties", [])}
        assert mapping.PROP_RULE_RUN_NOW in attached
        assert set(pre_wp52) <= attached  # nothing lost

    async def test_an_existing_condition_property_gains_the_manual_option(
        self, mock, client, repo
    ) -> None:
        # _seed_select_options runs on EVERY startup, find-or-create, so
        # spaces that predate the token get it without a migration.
        by_key = {p["key"]: p async for p in client.list_properties()}
        condition = by_key[mapping.PROP_RULE_CONDITION]
        options = {t["name"] async for t in client.list_tags(str(condition["id"]))}
        assert "Manual" in options
