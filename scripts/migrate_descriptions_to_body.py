"""One-shot migration: move gc_description property values into the body.

ADR 010 made the Anytype page body the node's description. Spaces written
before it hold the text in the retired ``gc_description`` property; until
migrated they are served by ``fetch_body``'s read fallback. This script
finishes the job for one space:

    for every live object with a non-empty gc_description:
        - body empty      -> PATCH the text in as `markdown`, then clear
                             the property (one combined write; A7)
        - body non-empty  -> SKIP and report (a human already wrote a body;
                             merging prose is a human call, not a script's)

Idempotent: a second run finds nothing to do. Writes pace at ~1/s (S7
throttle) via the client's 429 backoff plus an explicit sleep, so a large
space takes minutes -- run it when nothing else is writing.

Run (env: ANYTYPE_SPACE_ID, ANYTYPE_API_KEY or ANYTYPE_API_KEY_FILE,
ANYTYPE_API_BASE_URL if not localhost):

    PYTHONPATH=src python scripts/migrate_descriptions_to_body.py [--dry-run]
"""

import asyncio
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


async def migrate(client: AnytypeClient, *, dry_run: bool) -> tuple[int, int, int]:
    migrated = skipped = conflicts = 0
    candidates = [
        obj["id"] async for obj in client.list_objects() if _legacy_description(obj)
    ]
    print(f"{len(candidates)} object(s) carry a gc_description")
    for object_id in candidates:
        # Reads are unthrottled; fetch the authoritative single-object view
        # (list responses never include the body, A7).
        obj = await client.get_object(object_id)
        description = _legacy_description(obj)
        body = str(obj.get("markdown", "") or "")
        name = obj.get("name", object_id)
        if body.strip():
            conflicts += 1
            print(f"  CONFLICT {name!r}: body already written; left for a human")
            continue
        if dry_run:
            migrated += 1
            print(f"  would migrate {name!r} ({len(description)} chars)")
            continue
        await client.update_object(object_id, {
            "markdown": description,
            "properties": [property_entry(PROP_LEGACY_DESCRIPTION, "text", "")],
        })
        migrated += 1
        print(f"  migrated {name!r} ({len(description)} chars)")
        await asyncio.sleep(WRITE_PACING_SECONDS)
    return migrated, skipped, conflicts


async def main() -> None:
    dry_run = "--dry-run" in sys.argv[1:]
    config = AnytypeConfig.from_env()
    client = AnytypeClient(config)
    try:
        migrated, _, conflicts = await migrate(client, dry_run=dry_run)
    finally:
        await client.aclose()
    verb = "would migrate" if dry_run else "migrated"
    print(f"done: {verb} {migrated}, conflicts {conflicts}")
    if conflicts:
        print(
            "conflicted objects keep their gc_description; fetch_body serves "
            "the body (which outranks it) until a human merges the texts."
        )


if __name__ == "__main__":
    asyncio.run(main())
