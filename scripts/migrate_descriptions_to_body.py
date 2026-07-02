"""One-shot migration: move gc_description property values into the body.

ADR 010 made the Anytype page body the node's description. Spaces written
before it hold the text in the retired ``gc_description`` property; until
migrated they are served by ``fetch_body``'s read fallback. This script
finishes the job for one space. For every live object with a non-empty
gc_description:

    - body empty                    -> PATCH the text in as `markdown`,
                                       clearing the property in the same
                                       combined write (A7)
    - body already CONTAINS the     -> clear the property only; the body
      description (a human copied     is already the truth and stays
      it into the page)               byte-untouched
    - body holds DISTINCT text      -> SKIP and report (merging prose is
                                       a human call, not a script's)

Containment is judged on a whitespace/markdown-insensitive form: the
store normalizes markdown (S6), so byte comparison would misread every
copied body as distinct.

Idempotent: a second run finds nothing to do. Writes pace at ~1/s (S7
throttle) via the client's 429 backoff plus an explicit sleep, so a large
space takes minutes -- run it when nothing else is writing.

Run (env: ANYTYPE_SPACE_ID, ANYTYPE_API_KEY or ANYTYPE_API_KEY_FILE,
ANYTYPE_API_BASE_URL if not localhost):

    PYTHONPATH=src python scripts/migrate_descriptions_to_body.py [--dry-run]
"""

import asyncio
import re
import sys

from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.config import AnytypeConfig
from graph_context.infrastructure.anytype.mapping import (
    PROP_LEGACY_DESCRIPTION,
    property_entry,
)

WRITE_PACING_SECONDS = 1.1  # S7: ~1 write/s sustained; stay just under


def _legacy_description(obj: dict) -> str:
    for entry in obj.get("properties", []):
        if entry.get("key") == PROP_LEGACY_DESCRIPTION:
            return str(entry.get("text") or "")
    return ""


def _comparable(text: str) -> str:
    """Whitespace/markdown-insensitive form for containment checks (S6:
    the store normalizes markdown, so byte equality never holds)."""
    text = re.sub(r"[#*_>`\-\[\]()]", "", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _body_contains(body: str, description: str) -> bool:
    needle = _comparable(description)
    return bool(needle) and needle in _comparable(body)


async def migrate(client: AnytypeClient, *, dry_run: bool) -> tuple[int, int, int]:
    migrated = cleared = conflicts = 0
    candidates = [
        obj["id"] async for obj in client.list_objects() if _legacy_description(obj)
    ]
    print(f"{len(candidates)} object(s) carry a gc_description")
    clear_property = {
        "properties": [property_entry(PROP_LEGACY_DESCRIPTION, "text", "")]
    }
    for object_id in candidates:
        # Reads are unthrottled; fetch the authoritative single-object view
        # (list responses never include the body, A7).
        obj = await client.get_object(object_id)
        description = _legacy_description(obj)
        body = str(obj.get("markdown", "") or "")
        name = obj.get("name", object_id)
        if not body.strip():
            if dry_run:
                print(f"  would migrate {name!r} ({len(description)} chars)")
            else:
                await client.update_object(
                    object_id, {"markdown": description, **clear_property}
                )
                print(f"  migrated {name!r} ({len(description)} chars)")
                await asyncio.sleep(WRITE_PACING_SECONDS)
            migrated += 1
        elif _body_contains(body, description):
            if dry_run:
                print(f"  would clear stale copy on {name!r} (body already holds it)")
            else:
                await client.update_object(object_id, clear_property)
                print(f"  cleared stale copy on {name!r} (body already holds it)")
                await asyncio.sleep(WRITE_PACING_SECONDS)
            cleared += 1
        else:
            conflicts += 1
            print(f"  CONFLICT {name!r}: body holds distinct text; left for a human")
    return migrated, cleared, conflicts


async def main() -> None:
    dry_run = "--dry-run" in sys.argv[1:]
    config = AnytypeConfig.from_env()
    client = AnytypeClient(config)
    try:
        migrated, cleared, conflicts = await migrate(client, dry_run=dry_run)
    finally:
        await client.aclose()
    prefix = "would " if dry_run else ""
    print(
        f"done: {prefix}migrate {migrated}, {prefix}clear {cleared} stale "
        f"copies, conflicts {conflicts}"
    )
    if conflicts:
        print(
            "conflicted objects keep their gc_description; fetch_body serves "
            "the body (which outranks it) until a human merges the texts."
        )


if __name__ == "__main__":
    asyncio.run(main())
