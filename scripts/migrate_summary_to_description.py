"""One-shot migration: move gc_summary values into the built-in description.

ADR 011 made Anytype's built-in ``description`` property the summary
channel (UI-featured, hydratable). Spaces written before it hold the
one-liner in the retired ``gc_summary`` property; until migrated they are
served by ``to_node``'s read fallback. This script finishes the job for
one space. For every live object with a non-empty gc_summary:

    - built-in description empty     -> move the text over and clear
                                        gc_summary (one combined PATCH)
    - built-in already matches       -> clear gc_summary only (stale copy)
    - built-in holds DISTINCT text   -> SKIP and report (a human wrote a
                                        different one-liner; theirs wins
                                        only by their say-so)

Comparison is whitespace-insensitive. Unlike bodies there is no markdown
normalization on properties, but trailing-whitespace drift costs nothing
to tolerate.

Idempotent: a second run finds nothing to do. Writes pace at ~1/s (S7).

Run (env: ANYTYPE_SPACE_ID, ANYTYPE_API_KEY or ANYTYPE_API_KEY_FILE,
ANYTYPE_API_BASE_URL if not localhost):

    PYTHONPATH=src python scripts/migrate_summary_to_description.py [--dry-run]
"""

import asyncio
import sys

from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.config import AnytypeConfig
from graph_context.infrastructure.anytype.mapping import (
    PROP_LEGACY_SUMMARY,
    PROP_SUMMARY,
    property_entry,
)

WRITE_PACING_SECONDS = 1.1  # S7: ~1 write/s sustained; stay just under


def _text_prop(obj: dict, key: str) -> str:
    for entry in obj.get("properties", []):
        if entry.get("key") == key:
            return str(entry.get("text") or "")
    return ""


async def migrate(client: AnytypeClient, *, dry_run: bool) -> tuple[int, int, int]:
    moved = cleared = conflicts = 0
    clear_legacy = property_entry(PROP_LEGACY_SUMMARY, "text", "")
    # Both properties ride list responses (unlike bodies), so one sweep is
    # enough -- no per-object GETs needed.
    async for obj in client.list_objects():
        legacy = _text_prop(obj, PROP_LEGACY_SUMMARY)
        if not legacy:
            continue
        builtin = _text_prop(obj, PROP_SUMMARY)
        name = obj.get("name", obj.get("id", "?"))
        if not builtin.strip():
            if dry_run:
                print(f"  would move {name!r} ({len(legacy)} chars)")
            else:
                await client.update_object(obj["id"], {"properties": [
                    property_entry(PROP_SUMMARY, "text", legacy), clear_legacy,
                ]})
                print(f"  moved {name!r} ({len(legacy)} chars)")
                await asyncio.sleep(WRITE_PACING_SECONDS)
            moved += 1
        elif builtin.strip() == legacy.strip():
            if dry_run:
                print(f"  would clear stale copy on {name!r}")
            else:
                await client.update_object(obj["id"], {"properties": [clear_legacy]})
                print(f"  cleared stale copy on {name!r}")
                await asyncio.sleep(WRITE_PACING_SECONDS)
            cleared += 1
        else:
            conflicts += 1
            print(f"  CONFLICT {name!r}: built-in description holds distinct "
                  "text; left for a human")
    return moved, cleared, conflicts


async def main() -> None:
    dry_run = "--dry-run" in sys.argv[1:]
    config = AnytypeConfig.from_env()
    client = AnytypeClient(config)
    try:
        moved, cleared, conflicts = await migrate(client, dry_run=dry_run)
    finally:
        await client.aclose()
    prefix = "would " if dry_run else ""
    print(f"done: {prefix}move {moved}, {prefix}clear {cleared} stale copies, "
          f"conflicts {conflicts}")
    if conflicts:
        print(
            "conflicted objects keep their gc_summary for a human to resolve; "
            "the built-in description outranks it on read (re-run afterwards)."
        )


if __name__ == "__main__":
    asyncio.run(main())
