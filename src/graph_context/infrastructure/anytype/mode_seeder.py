"""Starter Activity Modes for a mode-less space (ADR 035).

Since ADR 035 the space's Activity Mode objects are the ONLY live source
of mode specs, so a space without any is unservable. This module heals
that exactly once: given seed payloads (ModeStore-port shaped, from
``interface/mode_config.seed_payloads`` -- this module never parses
TOML), it mints one object per seed plus the Example Mode explainer, and
links the seed marked ``default`` on the Space Context's
``gc_default_mode`` field. A space with ANY Activity Mode object is
never touched -- humans own the surface from the first object onward.

Seeding is startup config provisioning: failures raise (loudly), like
``ensure_schema``. A partial seed is recoverable -- the next startup
sees a non-empty space and skips the heal; finish by hand or archive the
partial seeds and restart.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from graph_context.domain.activity import DEFAULT_ACTIVITY_DETAIL
from graph_context.errors import GraphContextError
from graph_context.infrastructure.anytype import mapping
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.schema_bootstrap import (
    MODE_TYPE_KEY,
    SPACE_CONTEXT_TYPE_KEY,
)

logger = logging.getLogger(__name__)

# Fresh objects can lag type-scoped search (the settle window the mock
# does not model); the verify polls are bounded so a slow server fails
# loudly instead of hanging startup.
_SETTLE_ATTEMPTS = 20
_SETTLE_DELAY_SECONDS = 0.25

# The one-time template object whose body doubles as the feature's
# in-space documentation (moved here from schema_bootstrap at ADR 035:
# it is part of the starter kit, seeded with it). It loads as a valid
# read-only mode, so switching to it makes the model explain it is a
# template.
EXAMPLE_MODE_NAME = "Example Mode"
EXAMPLE_MODE_SUMMARY = (
    "Template: edit this page to define an assistant behavior; "
    "changes apply when /mode is next used in chat."
)
EXAMPLE_MODE_BODY = """\
Replace this page body with the mode's goal -- the instructions the \
assistant follows while this mode is active (for example: "Record only \
what the user explicitly states; organize and link it, but never invent \
or embellish details.").

How Activity Mode objects work:

- Every object of this type is one mode the assistant can switch into.
- The object name becomes the /mode name: "Example Mode" -> /mode example_mode.
- This page body is the goal prompt handed to the model.
- Tick gc_mode_mutating to let the mode create and update nodes; unticked \
means read-only.
- Pick a gc_mode_activity_detail option to control how much live \
progress the assistant streams into the chat while this mode works a \
turn: Off (just "Processing..." until the reply), Minimal (tool tally; \
the default when empty), Tools (each tool call), or Full (thinking and \
results too).
- Tick gc_mode_web_search to let the assistant search the web while this \
mode is active; unticked keeps it grounded in the graph alone.
- Pick a gc_mode_model option to run this mode on a specific Claude \
model (Sonnet 5, Opus 4.8, or Fable 5); empty uses the deployment's \
default model.
- Pick a gc_mode_thinking option to set how hard this mode thinks: a \
level (Low ... Max) enables thinking at that depth, Off disables it \
(not allowed with Fable 5, which always thinks), and empty uses the \
deployment default.
- Set gc_mode_max_tokens to cap one reply's output length in tokens; \
empty/0 uses the deployment default.
- With web search on: gc_mode_search_max_uses caps searches per turn, \
and gc_mode_search_allowed_domains / gc_mode_search_blocked_domains \
(comma or space separated) scope where it may search -- set at most one \
of the two lists.
- Fill gc_capture_type (and optionally gc_capture_references, \
gc_capture_min_chars) to auto-capture the assistant's substantial replies \
as objects of that type.
- Edits here do NOT apply on their own: send /mode in the chat to reload \
and list modes, or /mode <name> to switch.
- Archive an object to disable its mode.
- To make a mode the one NEW chats start in, link its object under \
Default mode on the space's Space Context object.
- These objects are the ONLY source of modes: archive or delete every \
one and the assistant reseeds its starter set at the next restart.
"""


async def seed_activity_modes(
    client: AnytypeClient, payloads: Sequence[Mapping[str, Any]]
) -> bool:
    """Seed starter Activity Mode objects into a mode-less space.

    Returns ``False`` untouched when the type-scoped search finds ANY
    Activity Mode object (archived ones never surface in search, so a
    fully-archived space heals too -- the archived originals stay
    archived). Otherwise mints the explainer plus one object per
    payload, waits for them to become searchable, and links the payload
    marked ``default`` on the Space Context -- only when that link is
    currently empty.
    """
    existing = [obj async for obj in client.search(types=[MODE_TYPE_KEY])]
    if existing:
        return False
    minted: dict[str, str] = {}  # payload id -> minted object id
    for payload in payloads:
        created = await client.create_object(
            await _create_payload(client, payload)
        )
        minted[str(payload["id"])] = str(created["id"])
    # The explainer mints LAST: a crash mid-seed then leaves real modes
    # behind (the next startup skips the heal), not just the template.
    await _seed_example_mode(client)
    await _await_searchable(client, frozenset(minted.values()))
    default = next((p for p in payloads if p.get("default")), None)
    if default is None and payloads:
        default = payloads[0]
    if default is not None:
        await _link_default(client, minted[str(default["id"])])
    logger.info(
        "seeded %d starter Activity Mode(s): %s",
        len(minted), ", ".join(str(p["name"]) for p in payloads),
    )
    return True


async def _create_payload(
    client: AnytypeClient, payload: Mapping[str, Any]
) -> dict[str, Any]:
    """One seed payload -> the create_object body.

    The exact inverse of ``AnytypeModeStore._payload``: the goal is the
    page body, the binding/capture ride the ``gc_mode_*`` /
    ``gc_capture_*`` properties. Selects are written only when they
    differ from the unset default, matching how human-authored mode
    objects normally look.
    """
    properties: list[dict[str, Any]] = [
        mapping.property_entry(
            mapping.PROP_MODE_MUTATING, "checkbox", bool(payload["mutating"])
        ),
        mapping.property_entry(
            mapping.PROP_MODE_WEB_SEARCH, "checkbox",
            bool(payload.get("web_search")),
        ),
    ]
    detail = str(payload.get("activity_detail") or "").strip()
    if detail and detail != DEFAULT_ACTIVITY_DETAIL:
        properties.append(mapping.property_entry(
            mapping.PROP_MODE_ACTIVITY_DETAIL, "select",
            await _tag_key(client, mapping.PROP_MODE_ACTIVITY_DETAIL, detail),
        ))
    model = str(payload.get("model") or "").strip()
    if model:
        properties.append(mapping.property_entry(
            mapping.PROP_MODE_MODEL, "select",
            await _tag_key(client, mapping.PROP_MODE_MODEL, model),
        ))
    # ADR 037 driver options, written only when set (unset = default).
    thinking = str(payload.get("thinking") or "").strip()
    if thinking:
        properties.append(mapping.property_entry(
            mapping.PROP_MODE_THINKING, "select",
            await _tag_key(client, mapping.PROP_MODE_THINKING, thinking),
        ))
    for key, prop in (
        ("max_tokens", mapping.PROP_MODE_MAX_TOKENS),
        ("web_search_max_uses", mapping.PROP_MODE_SEARCH_MAX_USES),
    ):
        if payload.get(key):
            properties.append(
                mapping.property_entry(prop, "number", int(payload[key]))
            )
    for key, prop in (
        ("web_search_allowed_domains", mapping.PROP_MODE_SEARCH_ALLOWED),
        ("web_search_blocked_domains", mapping.PROP_MODE_SEARCH_BLOCKED),
    ):
        if str(payload.get(key) or "").strip():
            properties.append(
                mapping.property_entry(prop, "text", str(payload[key]))
            )
    capture = payload.get("capture")
    if capture:
        properties.append(mapping.property_entry(
            mapping.PROP_CAPTURE_TYPE, "text", str(capture["artifact_type"])
        ))
        if capture.get("references_label"):
            properties.append(mapping.property_entry(
                mapping.PROP_CAPTURE_REFERENCES, "text",
                str(capture["references_label"]),
            ))
        if capture.get("min_chars") is not None:
            properties.append(mapping.property_entry(
                mapping.PROP_CAPTURE_MIN_CHARS, "number",
                int(capture["min_chars"]),
            ))
    body: dict[str, Any] = {
        "name": str(payload["name"]),
        "type_key": MODE_TYPE_KEY,
        "body": str(payload["goal"]),
        "properties": properties,
    }
    icon = str(payload.get("icon") or "")
    if icon:
        body["icon"] = {"format": "emoji", "emoji": icon}
    return body


async def _tag_key(client: AnytypeClient, property_key: str, value: str) -> str:
    """Resolve a select value to its tag key by option name.

    Bootstrap pre-seeds the options for both mode selects, so this
    normally just matches; a custom seed value that names no option is a
    config error and fails loudly naming the property.
    """
    infos = {p["key"]: p async for p in client.list_properties()}
    info = infos.get(property_key)
    if info is None:
        raise GraphContextError(
            f"cannot seed {property_key}: the property does not exist "
            "(did ensure_schema run?)"
        )
    target = value.strip().lower()
    async for tag in client.list_tags(str(info["id"])):
        if target in (
            str(tag.get("name", "")).lower(), str(tag.get("key", "")).lower()
        ):
            return str(tag["key"])
    raise GraphContextError(
        f"seed value {value!r} names no option of {property_key}; "
        "pick one of the property's options"
    )


async def _await_searchable(
    client: AnytypeClient, object_ids: frozenset[str]
) -> None:
    """Wait (bounded) until the minted objects surface in search.

    The consumer of the seed is ``AnytypeModeStore.load`` -- a
    type-scoped search -- so search visibility, not GET, is the bar.
    """
    if not object_ids:
        return
    for _ in range(_SETTLE_ATTEMPTS):
        found = {
            str(obj["id"])
            async for obj in client.search(types=[MODE_TYPE_KEY])
        }
        if object_ids <= found:
            return
        await asyncio.sleep(_SETTLE_DELAY_SECONDS)
    raise GraphContextError(
        "seeded Activity Mode objects did not become searchable in time; "
        "restart to retry (the seed itself is complete, so the next "
        "startup will load it)"
    )


async def _link_default(client: AnytypeClient, mode_object_id: str) -> None:
    """Point the Space Context's default-mode link at a seeded object.

    Only an EMPTY link is written -- an existing link is a human's
    choice, never clobbered. A missing Space Context object degrades to
    a warning (the loader then falls back to the alphabetically first
    mode).
    """
    for _ in range(_SETTLE_ATTEMPTS):
        hits = [
            obj async for obj in
            client.search(types=[SPACE_CONTEXT_TYPE_KEY])
        ]
        if hits:
            break
        await asyncio.sleep(_SETTLE_DELAY_SECONDS)
    else:
        logger.warning(
            "no Space Context object to link the default mode on; new "
            "chats fall back to the alphabetically first mode"
        )
        return
    for hit in hits:
        obj = await client.get_object(hit["id"])
        if obj.get("archived"):
            continue
        if mapping.relation_targets(obj, mapping.PROP_DEFAULT_MODE):
            return  # a human already chose
        await client.update_object(str(obj["id"]), {"properties": [
            mapping.property_entry(
                mapping.PROP_DEFAULT_MODE, "objects", [mode_object_id]
            ),
        ]})
        return


async def _seed_example_mode(client: AnytypeClient) -> None:
    """The template/explainer object (see EXAMPLE_MODE_BODY).

    Part of the starter kit since ADR 035 -- seeded with it, loudly (a
    space being provisioned should come up whole or fail visibly).
    """
    await client.create_object({
        "name": EXAMPLE_MODE_NAME,
        "type_key": MODE_TYPE_KEY,
        "body": EXAMPLE_MODE_BODY,
        "properties": [
            mapping.property_entry(
                mapping.PROP_SUMMARY, "text", EXAMPLE_MODE_SUMMARY
            ),
        ],
    })
