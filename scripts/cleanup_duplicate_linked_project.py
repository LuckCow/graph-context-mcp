"""One-time remediation for the ADR 047 incident space.

A near-duplicate ``linked_project`` / "Linked Project" relation was
minted beside Anytype's built-in ``linked_projects`` / "Linked
Projects" (turn ``e71063827519``, 2026-07-20) and spread over objects
while bare resolution was still space-wide. This script migrates every
object's ``linked_project`` targets onto ``linked_projects`` (union;
the duplicate's list is emptied in the same PATCH) and then deletes the
duplicate property -- ``DELETE /properties/:id`` is the only retirement
the local API offers (soft-delete + detach, the same call bootstrap's
retrofit uses).

Idempotent: a space without the duplicate property is a no-op, and an
object already migrated plans nothing. The deletion runs only when
every migration succeeded; any failure degrades to the printed report.

Run:  PYTHONPATH=src python scripts/cleanup_duplicate_linked_project.py \
          --space-id <space id> [--apply]
Env:  the usual ANYTYPE_API_* connection variables. Without ``--apply``
      it prints the plan and changes nothing.
"""

import argparse
import asyncio

from graph_context.infrastructure.anytype import mapping
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.config import AnytypeConfig

DUP_KEY = "linked_project"
CANON_KEY = "linked_projects"


async def cleanup(space_id: str, apply: bool) -> None:
    client = AnytypeClient(AnytypeConfig.from_env(space_id))
    try:
        properties = {
            p.get("key"): p async for p in client.list_properties()
        }
        duplicate = properties.get(DUP_KEY)
        canonical = properties.get(CANON_KEY)
        if duplicate is None:
            print(f"no {DUP_KEY!r} property in the space; nothing to do")
            return
        if canonical is None:
            print(
                f"refusing: {DUP_KEY!r} exists but the canonical "
                f"{CANON_KEY!r} does not -- nowhere to migrate"
            )
            return

        plans: list[tuple[str, str, list[str]]] = []  # id, name, union
        async for obj in client.list_objects():
            dup_targets = mapping.relation_targets(obj, DUP_KEY)
            if not dup_targets:
                continue
            canon_targets = mapping.relation_targets(obj, CANON_KEY)
            union = list(canon_targets)
            union.extend(t for t in dup_targets if t not in canon_targets)
            plans.append((obj["id"], str(obj.get("name") or obj["id"]), union))

        if not plans:
            print(f"no object carries {DUP_KEY!r} targets")
        failures = 0
        for object_id, name, union in plans:
            line = f"{name} ({object_id}): {CANON_KEY} <- {len(union)} target(s)"
            if not apply:
                print(f"would migrate {line}")
                continue
            payload = {"properties": [
                mapping.property_entry(CANON_KEY, "objects", union),
                mapping.property_entry(DUP_KEY, "objects", []),
            ]}
            try:
                await client.update_object(object_id, payload)
            except Exception as err:  # noqa: BLE001 -- report-and-continue boundary
                failures += 1
                print(f"FAILED to migrate {line}: {err}")
            else:
                print(f"migrated {line}")

        if not apply:
            print(
                f"dry run: {len(plans)} object(s) to migrate, then delete "
                f"property {DUP_KEY!r} ({duplicate.get('id')}); rerun with "
                "--apply"
            )
            return
        if failures:
            print(
                f"{failures} migration(s) failed; leaving property "
                f"{DUP_KEY!r} in place -- fix and rerun"
            )
            return
        await client.delete_property(str(duplicate["id"]))
        print(f"deleted duplicate property {DUP_KEY!r} ({duplicate.get('id')})")
    finally:
        await client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--space-id", required=True)
    parser.add_argument(
        "--apply", action="store_true",
        help="perform the migration + deletion (default: dry run)",
    )
    args = parser.parse_args()
    asyncio.run(cleanup(args.space_id, args.apply))


if __name__ == "__main__":
    main()
