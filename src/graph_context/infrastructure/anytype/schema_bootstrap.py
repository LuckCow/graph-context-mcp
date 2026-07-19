"""Idempotent schema bootstrap: ensure our *infrastructure* exists.

The space-reflecting model does NOT create a type per node kind anymore --
story entities use the user's own native Anytype types. Bootstrap is now
infra-only and create-if-missing:

* the ``gc_`` types we still own (gc_prose capture artifacts -- display
  name "Capture" since ADR 015 -- the managed SessionContext node, and
  gc_intent provenance records);
* the scalar ``gc_`` properties we write onto native objects (stale flag,
  story-time), the session-state slots (key + snapshot), and the ADR 028
  attribution properties the recorders stamp onto intent/capture nodes.
  Not minted here: descriptions live in the object body (ADR 010), and
  summaries live in the **built-in** ``description`` property every space
  already has (ADR 011);
* a small starter vocabulary of ``gc_edge_*`` relations so the model has
  reusable relation labels for common links without an approval round-trip.
  Human-created relations (``boss``, ``triggered_by``, ...) are first-class
  too; these are merely convenient defaults.

Safe to re-run: existing keys are left untouched. Only the first run against
a space creates anything.
"""

from __future__ import annotations

import logging
from typing import Any

from graph_context.domain import attribution
from graph_context.domain.schema import Role
from graph_context.infrastructure.anytype import mapping
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.config import AnytypeApiError

logger = logging.getLogger(__name__)

# gc_ infrastructure types: (key, display name) for the role.
PROSE_TYPE_KEY = "gc_prose"
SESSION_TYPE_KEY = "gc_session_context"
INTENT_TYPE_KEY = "gc_intent"  # WP7/ADR 008: one provenance node per turn
MODE_TYPE_KEY = "gc_activity_mode"  # ADR 015 amendment: in-space mode config
SCHEDULED_TYPE_KEY = "gc_scheduled_event"  # WP18/ADR 027: timed prompts
SPACE_CONTEXT_TYPE_KEY = "gc_space_context"  # ADR 034: space-wide settings
RULE_TYPE_KEY = "gc_rule"  # WP31/ADR 039: reactive automations
INFRA_TYPES: dict[str, str] = {
    PROSE_TYPE_KEY: Role.CAPTURE.value,
    SESSION_TYPE_KEY: Role.SESSION_CONTEXT.value,
    INTENT_TYPE_KEY: Role.INTENT.value,
    MODE_TYPE_KEY: "Activity Mode",
    SCHEDULED_TYPE_KEY: "Scheduled Event",
    SPACE_CONTEXT_TYPE_KEY: "Space Context",
    RULE_TYPE_KEY: "Automation Rule",
}

# Types whose fields humans edit in the Anytype UI get their properties
# attached INLINE at type creation, so the object editor shows the fields
# (live-confirmed 2026-07-06). The properties also register space-wide, so
# ensure_schema's property loops skip them on a fresh mint.
_INLINE_TYPE_PROPERTIES: dict[str, dict[str, str]] = {
    MODE_TYPE_KEY: mapping.MODE_PROPERTIES,
    SCHEDULED_TYPE_KEY: {
        **mapping.SCHEDULED_PROPERTIES,
        # Reused across session + scheduled nodes; inline here so a human
        # creating a Scheduled Event sees the delivery-target field too.
        **mapping.SESSION_PROPERTIES,
    },
    # Attribution stamps (ADR 028) inline on the recorder-written types,
    # so an Intent/Capture opened in the editor shows its provenance
    # fields. gc_prose only carries the timestamp.
    INTENT_TYPE_KEY: mapping.ATTRIBUTION_PROPERTIES,
    PROSE_TYPE_KEY: {
        attribution.FIELD_GENERATED_AT:
            mapping.ATTRIBUTION_PROPERTIES[attribution.FIELD_GENERATED_AT],
    },
    SESSION_TYPE_KEY: {
        **mapping.SESSION_PROPERTIES,
        **mapping.SESSION_STATE_PROPERTIES,
    },
    SPACE_CONTEXT_TYPE_KEY: mapping.SPACE_CONTEXT_PROPERTIES,
    RULE_TYPE_KEY: mapping.RULE_PROPERTIES,
}

# The Activity Mode explainer/template moved to mode_seeder.py (ADR 035):
# it is part of the starter-mode kit, seeded whenever a mode-less space
# is healed -- no longer tied to the type mint.

# Seeded once, when the Scheduled Event type is first minted (ADR 027):
# the same explainer pattern as the example mode. Its schedule is left
# EMPTY, so it can never fire; a human copies the recipe, the LLM uses
# the `schedule` tool.
EXAMPLE_EVENT_NAME = "Example Scheduled Event"
EXAMPLE_EVENT_SUMMARY = (
    "Template: fill Schedule and Schedule prompt to make the assistant "
    "check in on its own; this example never fires (its Schedule is empty)."
)
EXAMPLE_EVENT_BODY = """\
A Scheduled Event makes the assistant start a chat turn on its own at a \
time you choose, following the instructions you store here.

Fields:

- Schedule -- WHEN, in the server's local time. Either a one-shot ISO \
date-time like 2027-04-08T09:00 (fires once), or a 5-field cron line \
"minute hour day month weekday" like 0 9 * * 1 (Mondays 09:00; weekday \
0 and 7 are Sunday).
- Schedule prompt -- the instructions the assistant receives when it \
fires. Write them self-contained, e.g. "Remind Nick that taxes are due \
April 15 and ask whether he has filed."
- Schedule status -- Pending events fire; set Completed or Cancelled to \
stop one, or back to Pending to re-enable it. Empty counts as Pending. \
The assistant marks a one-shot Completed after it fires.
- Last fired -- bookkeeping, written by the assistant.
- Session key -- optional: which chat the fired turn speaks into \
(anytype:<chat id>). Empty delivers to the space's first served chat.

You can also just ask the assistant in chat ("remind me a week before \
taxes are due") -- it creates these objects itself.
"""

# Seeded once, when the Automation Rule type is first minted (WP31,
# ADR 039): the explainer pattern again. Its config is left EMPTY -- an
# unconfigured rule is skipped silently, so it can never run; the body
# documents every field and both canonical recipes.
EXAMPLE_RULE_NAME = "Example Automation Rule"
EXAMPLE_RULE_SUMMARY = (
    "Template: fill the Rule fields to make the assistant react to "
    "property changes; this example never runs (its config is empty)."
)
EXAMPLE_RULE_BODY = """\
An Automation Rule makes the assistant react on its own when a property \
changes on objects of one type -- it fires on the CHANGE, never on a \
value that was already there when the assistant started.

Fields:

- Rule target type -- which object type to watch, e.g. Task.
- Rule watch property -- the property whose change triggers the rule, \
by its display name, e.g. Done. Checkbox and select properties work \
best; a text property saves as you type, so "Changed" can fire on a \
half-typed value.
- Rule condition -- Changed to true, Changed to false, or Changed.
- Rule action -- what happens:
  - Set property to now writes the current date-time into the Rule \
action property (e.g. Completion date). A date-format property gets \
the date only; use a text property if you want the time of day.
  - Set property value writes the Rule action value into the Rule \
action property.
  - Uncheck others of type keeps a checkbox exclusive: when it is \
ticked on one object, it is unticked on every other object of the \
type. Leave Rule condition and Rule action property empty for this one.
  - Run script runs the Python code block in THIS page's body (see \
Recipe 3) in a sandbox.
- Rule status -- Active rules run; set Paused to switch one off. Empty \
counts as Active. The assistant sets Error (with Rule last error) when \
a rule is misconfigured, and flips it back to Active once it is fixed.
- Rule last fired / Rule last error -- bookkeeping, written by the \
assistant.

Recipe 1 -- stamp completion time: target type Task, watch property \
Done, condition Changed to true, action Set property to now, action \
property Completion date.

Recipe 2 -- one default at a time: target type Project, watch property \
Default, action Uncheck others of type.

Recipe 3 -- a script (action Run script, condition Changed): put a \
python code block in the rule page's body, like this one, which keeps \
an open-task count on a project:

```python
open_tasks = [t for t in objects(type="Task")
              if field(t, "Done") != "true"]
project = find("Roadmap", type="Project")
if project:
    set(project, "Open tasks", len(open_tasks))
    log(f"{len(open_tasks)} open tasks")
```

The script sees: trigger (the changed object as a dict), before/after \
(the watched value around the change; empty means unset), now (the \
current local date-time as text -- use it instead of the clock), \
objects(type=None), find(name, type=None), field(obj, prop), \
neighbors(obj, edge_type=None) to read the space, set(obj, prop, \
value) to queue writes (at most 20 per fire; the property must \
already exist), and log(msg) for the assistant's log (print output \
is discarded). No imports beyond Python's standard library, no \
network, about 5 seconds of run time, and spaces over 2000 objects \
are too large for scripts. Text properties save as you type, so \
prefer checkbox or select watch properties.
"""

# Seeded once, when the Space Context type is first minted (ADR 034).
# Unlike the two explainers above, this object IS the config surface: the
# loader reads its gc_default_mode link on every registry (re)load. Its
# body documents itself; deleting it just returns new chats to the
# profile's built-in default mode.
SPACE_CONTEXT_NAME = "Space Context"
SPACE_CONTEXT_SUMMARY = (
    "Space-wide assistant settings: link an Activity Mode under "
    "Default mode to pick what NEW chats start in."
)
SPACE_CONTEXT_BODY = """\
This object holds space-wide assistant settings; the assistant re-reads \
it whenever modes reload (send /mode in a chat to apply edits).

- Default mode -- link exactly ONE Activity Mode object here to make it \
the mode NEW chats start in. Chats that already picked a mode with \
/mode keep their choice. Leave the link empty to use the assistant's \
built-in default.
- Keep exactly one Space Context object in the space -- a second one is \
a configuration error the assistant reports instead of guessing.
- Deleting this object simply returns new chats to the built-in \
default; create a new Space Context object to set one again.
"""

# Starter relation vocabulary (key, display name). Reusable defaults; the
# space-reflecting reader also picks up any human-created relation.
DEFAULT_EDGE_RELATIONS: list[tuple[str, str]] = [
    ("gc_edge_knows", "edge: knows"),
    ("gc_edge_located_at", "edge: located_at"),
    ("gc_edge_member_of", "edge: member_of"),
    ("gc_edge_participated_in", "edge: participated_in"),
    ("gc_edge_caused", "edge: caused"),
    ("gc_edge_possesses", "edge: possesses"),
    ("gc_edge_parent_of", "edge: parent_of"),
    ("gc_edge_child_of", "edge: child_of"),
    ("gc_edge_references", "edge: references"),
    ("gc_edge_precedes", "edge: precedes"),
    ("gc_edge_intent", "edge: intent"),  # intent node -> touched node (WP7)
]


async def ensure_schema(
    client: AnytypeClient,
    timeline: tuple[str, str] = mapping.DEFAULT_TIMELINE,
) -> None:
    """Create any missing gc_ infrastructure types and properties.

    ``timeline`` is the profile-declared Event-timeline property (ADR 015);
    a native date key an assistant profile names may not exist yet in a
    fresh space, and an inline create naming an unknown property 400s.
    """
    existing_types = {t["key"]: t async for t in client.list_types()}
    existing_properties = {p["key"]: p async for p in client.list_properties()}
    await _heal_select_formats(client, existing_properties)
    for key, name in INFRA_TYPES.items():
        if key not in existing_types:
            logger.info("bootstrap: creating infra type %s", key)
            payload: dict[str, Any] = {
                "key": key,
                "name": name,
                "plural_name": f"{name}s",
                "layout": "basic",
            }
            # Only NOT-yet-existing keys go inline: attaching an already-
            # minted space property inline is unverified against the live
            # server (upgraded spaces hit this; fresh spaces inline all).
            inline = [
                {"key": prop, "name": _display_name(prop), "format": fmt}
                for prop, fmt in _INLINE_TYPE_PROPERTIES.get(key, {}).items()
                if prop not in existing_properties
            ]
            if inline:
                payload["properties"] = inline
                existing_properties.update(
                    {entry["key"]: entry for entry in inline}
                )
            await client.create_type(payload)
            if key == SCHEDULED_TYPE_KEY:
                await _seed_example_event(client)
            if key == SPACE_CONTEXT_TYPE_KEY:
                await _seed_space_context(client)
            if key == RULE_TYPE_KEY:
                await _seed_example_rule(client)
        else:
            # Upgraded-space path: the type predates a field added to its
            # inline set (e.g. WP19's gc_mode_activity_detail) -- attach
            # the missing ones so humans see the field in the editor.
            await _retrofit_type_fields(
                client, existing_types[key],
                _INLINE_TYPE_PROPERTIES.get(key, {}), existing_properties,
            )

    timeline_key, timeline_format = timeline
    if timeline_key not in existing_properties:
        logger.info("bootstrap: creating timeline property %s (%s)",
                    timeline_key, timeline_format)
        existing_properties[timeline_key] = await client.create_property(
            {"key": timeline_key, "name": timeline_key, "format": timeline_format}
        )
    for key, fmt in mapping.SCALAR_PROPERTIES.items():
        if key not in existing_properties:
            logger.info("bootstrap: creating property %s (%s)", key, fmt)
            await client.create_property({"key": key, "name": key, "format": fmt})
    # Mode-object fields (ADR 015 amendment). A fresh mint above creates
    # them inline with the type; this covers spaces where the type predates
    # a property (partial creation, older servers).
    for key, fmt in mapping.MODE_PROPERTIES.items():
        if key not in existing_properties:
            logger.info("bootstrap: creating property %s (%s)", key, fmt)
            await client.create_property({"key": key, "name": key, "format": fmt})
    # Session discriminator + snapshot slot (WP8/ADR 021, ADR 028): live
    # only on session nodes (the key also on scheduled events since ADR
    # 027, as the delivery target).
    for key, fmt in {
        **mapping.SESSION_PROPERTIES, **mapping.SESSION_STATE_PROPERTIES,
    }.items():
        if key not in existing_properties:
            logger.info("bootstrap: creating property %s (%s)", key, fmt)
            await client.create_property(
                {"key": key, "name": _display_name(key), "format": fmt}
            )
    # Scheduled Event fields (WP18/ADR 027), attribution stamps (ADR 028),
    # and the Space Context link (ADR 034): same coverage posture as the
    # mode fields -- a fresh mint attaches them inline with the type.
    for key, fmt in {
        **mapping.SCHEDULED_PROPERTIES, **mapping.ATTRIBUTION_PROPERTIES,
        **mapping.SPACE_CONTEXT_PROPERTIES, **mapping.RULE_PROPERTIES,
    }.items():
        if key not in existing_properties:
            logger.info("bootstrap: creating property %s (%s)", key, fmt)
            await client.create_property(
                {"key": key, "name": _display_name(key), "format": fmt}
            )
    for key, name in DEFAULT_EDGE_RELATIONS:
        if key not in existing_properties:
            logger.info("bootstrap: creating relation property %s", key)
            await client.create_property({"key": key, "name": name, "format": "objects"})
    await _seed_select_options(client)


async def _heal_select_formats(
    client: AnytypeClient, existing_properties: dict[str, dict[str, Any]]
) -> None:
    """Re-mint infra selects that were born under an older format.

    Quirk A12: a property's format is immutable (PATCH silently keeps the
    old one), so the only migration is delete + re-create under the same
    key -- deleting detaches the field from its types, and the retrofit /
    mint steps that follow re-attach it as a select. Values set under the
    old format are lost with it (acceptable: they were about to become
    unreadable anyway; this ran once, for gc_mode_activity_detail's
    one-day life as a text property).
    """
    for key in mapping.SELECT_OPTIONS:
        entry = existing_properties.get(key)
        if entry is None or entry.get("format") == "select":
            continue
        logger.info(
            "bootstrap: property %s exists as %r but must be a select; "
            "re-minting (A12)", key, entry.get("format"),
        )
        await client.delete_property(str(entry["id"]))
        del existing_properties[key]


async def _seed_select_options(client: AnytypeClient) -> None:
    """Pre-seed the dropdown options of select-format infra properties,
    so humans pick a value instead of typing the enum (WP19 amendment).

    Find-or-create by case-insensitive name, create-only: renames and
    recolors made in the UI are human-owned and never clobbered. Options
    someone added beyond ours are left alone (the loader rejects unknown
    values naming the object, so a stray option fails loudly when used).
    """
    if not mapping.SELECT_OPTIONS:
        return
    by_key = {p["key"]: p async for p in client.list_properties()}
    for key, options in mapping.SELECT_OPTIONS.items():
        info = by_key.get(key)
        if info is None:
            continue  # never minted: nothing to decorate
        have = {
            str(tag.get("name", "")).lower()
            async for tag in client.list_tags(str(info["id"]))
        }
        for name in options:
            if name.lower() in have:
                continue
            logger.info("bootstrap: seeding option %r on %s", name, key)
            await client.create_tag(
                str(info["id"]),
                {"name": name, "color": mapping.tag_color(name)},
            )


def _display_name(key: str) -> str:
    """A property's human-facing mint name (people edit these fields in
    the Anytype editor); keys without one mint under the raw key. Mint
    time only -- names are human-owned afterwards."""
    return mapping.PROPERTY_DISPLAY_NAMES.get(key, key)


async def _retrofit_type_fields(
    client: AnytypeClient,
    listed: dict[str, Any],
    expected: dict[str, str],
    existing_properties: dict[str, dict[str, Any]],
) -> None:
    """Attach newly-added infra fields to a type that predates them.

    Quirk A11 (spike_type_update, 2026-07-15): the type-update's
    ``properties`` list replaces the human-managed fields WHOLESALE, so
    the type's full fetched list rides along with the additions; name,
    plural_name, and layout are resent verbatim (human-owned). No-ops
    when nothing is missing, so a normal startup makes one extra GET per
    infra type and zero writes.
    """
    fetched = await client.get_type(listed["id"])
    current = fetched.get("properties", [])
    have = {entry.get("key") for entry in current}
    missing = [key for key in expected if key not in have]
    if not missing:
        return
    logger.info(
        "bootstrap: attaching %s to existing type %s",
        ", ".join(missing), fetched.get("key", listed["id"]),
    )
    added = [
        {"key": key, "name": _display_name(key), "format": expected[key]}
        for key in missing
    ]
    await client.update_type(listed["id"], {
        "name": fetched["name"],
        "plural_name": fetched.get("plural_name") or f"{fetched['name']}s",
        "layout": fetched.get("layout", "basic"),
        "properties": [
            {"key": e["key"], "name": e["name"], "format": e["format"]}
            for e in current
        ] + added,
    })
    existing_properties.update({entry["key"]: entry for entry in added})


async def _seed_example_event(client: AnytypeClient) -> None:
    """The Scheduled Event explainer object (see EXAMPLE_EVENT_BODY).

    Best-effort, like the example mode. The schedule is left empty so it
    can never fire; the prompt shows what one looks like.
    """
    try:
        await client.create_object({
            "name": EXAMPLE_EVENT_NAME,
            "type_key": SCHEDULED_TYPE_KEY,
            "body": EXAMPLE_EVENT_BODY,
            "icon": {"format": "emoji", "emoji": "⏰"},
            "properties": [
                mapping.property_entry(
                    mapping.PROP_SUMMARY, "text", EXAMPLE_EVENT_SUMMARY
                ),
                mapping.property_entry(
                    mapping.PROP_SCHEDULE_PROMPT, "text",
                    "Remind Nick that taxes are due April 15 and ask "
                    "whether he has filed.",
                ),
            ],
        })
    except AnytypeApiError:
        logger.warning(
            "bootstrap: could not seed the example Scheduled Event",
            exc_info=True,
        )


async def _seed_example_rule(client: AnytypeClient) -> None:
    """The Automation Rule explainer object (see EXAMPLE_RULE_BODY).

    Best-effort, like the other explainers. Its config is empty, and an
    unconfigured rule is skipped silently -- it can never run.
    """
    try:
        await client.create_object({
            "name": EXAMPLE_RULE_NAME,
            "type_key": RULE_TYPE_KEY,
            "body": EXAMPLE_RULE_BODY,
            "icon": {"format": "emoji", "emoji": "⚡"},
            "properties": [
                mapping.property_entry(
                    mapping.PROP_SUMMARY, "text", EXAMPLE_RULE_SUMMARY
                ),
            ],
        })
    except AnytypeApiError:
        logger.warning(
            "bootstrap: could not seed the example Automation Rule",
            exc_info=True,
        )


async def _seed_space_context(client: AnytypeClient) -> None:
    """The Space Context singleton (ADR 034), seeded with an empty link.

    Best-effort like the explainers: without it new chats simply start in
    the profile's default mode, and a human can create the object by hand
    (the loader treats any single non-archived gc_space_context object as
    THE settings surface).
    """
    try:
        await client.create_object({
            "name": SPACE_CONTEXT_NAME,
            "type_key": SPACE_CONTEXT_TYPE_KEY,
            "body": SPACE_CONTEXT_BODY,
            "icon": {"format": "emoji", "emoji": "⚙️"},
            "properties": [
                mapping.property_entry(
                    mapping.PROP_SUMMARY, "text", SPACE_CONTEXT_SUMMARY
                ),
            ],
        })
    except AnytypeApiError:
        logger.warning(
            "bootstrap: could not seed the Space Context object", exc_info=True
        )


