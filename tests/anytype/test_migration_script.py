"""The ADR 010 migration script: gc_description -> body, mock-backed.

The script is loaded by file path (scripts/ is not a package); its
``migrate(client, dry_run=...)`` core is exercised against MockAnytype
with an instant sleep so pacing costs nothing in CI.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from typing import Any

import pytest

from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.mock_server import MockAnytype

_SCRIPT = Path(__file__).parents[2] / "scripts" / "migrate_descriptions_to_body.py"

spec = importlib.util.spec_from_file_location("migrate_script", _SCRIPT)
assert spec and spec.loader
migrate_script: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migrate_script)


@pytest.fixture(autouse=True)
def instant_pacing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(migrate_script, "WRITE_PACING_SECONDS", 0)


def _seed_legacy(mock: MockAnytype, name: str, description: str, *, body: str = "") -> str:
    return mock.seed_object(
        "location", name,
        properties=[{"key": "gc_description", "format": "text", "text": description}],
        body=body,
    )


async def test_moves_description_into_body_and_clears_property(
    mock: MockAnytype, client: AnytypeClient
) -> None:
    await client.create_type(
        {"key": "location", "name": "Location", "plural_name": "Locations",
         "layout": "basic"}
    )
    object_id = _seed_legacy(mock, "Old Keep", "Pre-migration text.")
    migrated, _, conflicts = await migrate_script.migrate(client, dry_run=False)
    assert (migrated, conflicts) == (1, 0)
    obj = await client.get_object(object_id)
    props = {p["key"]: p.get("text") for p in obj["properties"]}
    assert obj["markdown"] == "Pre-migration text."
    assert props["gc_description"] == ""
    # Idempotent: a second run finds nothing.
    assert await migrate_script.migrate(client, dry_run=False) == (0, 0, 0)


async def test_distinct_body_is_a_conflict_left_untouched(
    mock: MockAnytype, client: AnytypeClient
) -> None:
    object_id = _seed_legacy(
        mock, "Keep", "Old property text.", body="A human already wrote this."
    )
    migrated, cleared, conflicts = await migrate_script.migrate(client, dry_run=False)
    assert (migrated, cleared, conflicts) == (0, 0, 1)
    obj = await client.get_object(object_id)
    props = {p["key"]: p.get("text") for p in obj["properties"]}
    assert obj["markdown"] == "A human already wrote this."
    assert props["gc_description"] == "Old property text."  # kept for the human


async def test_body_containing_the_description_clears_the_stale_copy(
    mock: MockAnytype, client: AnytypeClient
) -> None:
    """The dominant real-space case: a human already copied the description
    into the page body (modulo markdown normalization). The property is a
    stale duplicate -- cleared; the body stays byte-untouched."""
    object_id = _seed_legacy(
        mock, "Keep", "The old keep\nstands alone.",
        body="# The old keep stands alone.   \n",
    )
    migrated, cleared, conflicts = await migrate_script.migrate(client, dry_run=False)
    assert (migrated, cleared, conflicts) == (0, 1, 0)
    obj = await client.get_object(object_id)
    props = {p["key"]: p.get("text") for p in obj["properties"]}
    assert obj["markdown"] == "# The old keep stands alone.   \n"  # untouched
    assert props["gc_description"] == ""
    # Idempotent: nothing left on a second pass.
    assert await migrate_script.migrate(client, dry_run=False) == (0, 0, 0)


async def test_dry_run_writes_nothing(
    mock: MockAnytype, client: AnytypeClient
) -> None:
    object_id = _seed_legacy(mock, "Keep", "Text.")
    writes_before = [entry for entry in mock.request_log if entry[0] == "PATCH"]
    migrated, _, _ = await migrate_script.migrate(client, dry_run=True)
    assert migrated == 1
    assert [e for e in mock.request_log if e[0] == "PATCH"] == writes_before
    assert (await client.get_object(object_id))["markdown"] == ""


async def test_pacing_sleeps_between_writes(
    mock: MockAnytype, client: AnytypeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The S7 write throttle is respected: one sleep per migrated object."""
    _seed_legacy(mock, "A", "a")
    _seed_legacy(mock, "B", "b")
    monkeypatch.setattr(migrate_script, "WRITE_PACING_SECONDS", 1.1)
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def recording_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(migrate_script.asyncio, "sleep", recording_sleep)
    migrated, _, _ = await migrate_script.migrate(client, dry_run=False)
    assert migrated == 2
    # The patch reaches the shared asyncio module, so the mock transport's
    # yield-to-loop sleep(0)s are recorded too; the pacing sleeps are the
    # non-zero ones.
    assert [s for s in sleeps if s] == [1.1, 1.1]
