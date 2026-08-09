"""One-time retrofit: mint the Space Setup mode into already-seeded spaces.

The mode seeder is seed-once (ADR 035): a space with ANY Activity Mode
object is never touched, so live spaces that predate the Space Setup
starter mode (ADR 045) never receive it. This script closes that gap
exactly once: for every binding in the spaces file it runs
``ensure_schema`` (minting ``gc_mode_meta_inspection`` if missing), then
creates the ``space_setup`` seed's object -- skipping any space that
already has a mode whose name slugifies to ``space_setup``, so reruns
are no-ops.

It deliberately never touches ``gc_default_mode``: which mode NEW chats
start in stays the human's choice on the Space Context object (ADR 034).
After running, ``/mode`` in each space lists space_setup (the ADR 044
change tick also picks it up within seconds on a running bot).

Run:  PYTHONPATH=src python scripts/seed_space_setup_mode.py
Env:  GC_SPACES_FILE (default spaces.toml), GC_PROFILE, and the usual
      ANYTYPE_API_* connection variables.
"""

import asyncio
import os

from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.config import AnytypeConfig
from graph_context.infrastructure.anytype.mode_seeder import create_payload
from graph_context.infrastructure.anytype.schema_bootstrap import (
    MODE_TYPE_KEY,
    ensure_schema,
)
from graph_context.interface import mode_config
from graph_context.orchestrator.spaces import load_space_bindings

TARGET_SLUG = "space_setup"


async def seed_binding(space_id: str, profile_name: str,
                       modes_file: str | None, project: str) -> None:
    seeds = mode_config.load_seed_modes(modes_file, profile_name)
    payload = next(
        (p for p in mode_config.seed_payloads(seeds)
         if mode_config.slugify(str(p["name"])) == TARGET_SLUG),
        None,
    )
    if payload is None:
        print(f"{project}: seed corpus has no {TARGET_SLUG} mode; skipped")
        return
    config = AnytypeConfig.from_env(space_id)
    client = AnytypeClient(config)
    try:
        await ensure_schema(client)
        existing = [obj async for obj in client.search(types=[MODE_TYPE_KEY])]
        slugs = {
            mode_config.slugify(str(obj.get("name") or "")) for obj in existing
        }
        if TARGET_SLUG in slugs:
            print(f"{project}: {TARGET_SLUG} already present; skipped")
            return
        created = await client.create_object(
            await create_payload(client, payload)
        )
        print(f"{project}: minted {payload['name']} ({created['id']})")
    finally:
        await client.aclose()


async def main() -> None:
    path = os.environ.get("GC_SPACES_FILE", "spaces.toml")
    bindings = load_space_bindings(path, os.environ.get("GC_PROFILE"))
    for binding in bindings:
        await seed_binding(
            binding.space_id, binding.profile.name,
            binding.modes_file, binding.project or binding.space_id,
        )


if __name__ == "__main__":
    asyncio.run(main())
