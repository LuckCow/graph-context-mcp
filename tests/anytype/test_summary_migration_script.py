"""The ADR 011 migration script: gc_summary -> built-in description."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.mock_server import MockAnytype

_SCRIPT = Path(__file__).parents[2] / "scripts" / "migrate_summary_to_description.py"

spec = importlib.util.spec_from_file_location("summary_migrate_script", _SCRIPT)
assert spec and spec.loader
migrate_script: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migrate_script)


@pytest.fixture(autouse=True)
def instant_pacing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(migrate_script, "WRITE_PACING_SECONDS", 0)


def _seed(mock: MockAnytype, name: str, *, legacy: str = "", builtin: str = "") -> str:
    properties = []
    if legacy:
        properties.append({"key": "gc_summary", "format": "text", "text": legacy})
    if builtin:
        properties.append({"key": "description", "format": "text", "text": builtin})
    return mock.seed_object("character", name, properties=properties)


def _texts(obj: dict) -> dict[str, str]:
    return {
        p["key"]: p.get("text")
        for p in obj["properties"]
        if p.get("format") == "text"
    }


async def test_moves_summary_and_clears_legacy(
    mock: MockAnytype, client: AnytypeClient
) -> None:
    object_id = _seed(mock, "Mira", legacy="Exiled engineer.")
    assert await migrate_script.migrate(client, dry_run=False) == (1, 0, 0)
    texts = _texts(await client.get_object(object_id))
    assert texts["description"] == "Exiled engineer."
    assert texts["gc_summary"] == ""
    # Idempotent: nothing on a second pass.
    assert await migrate_script.migrate(client, dry_run=False) == (0, 0, 0)


async def test_matching_builtin_clears_the_stale_copy(
    mock: MockAnytype, client: AnytypeClient
) -> None:
    object_id = _seed(mock, "Mira", legacy="Same line.", builtin="Same line. ")
    assert await migrate_script.migrate(client, dry_run=False) == (0, 1, 0)
    texts = _texts(await client.get_object(object_id))
    assert texts["description"] == "Same line. "  # untouched
    assert texts["gc_summary"] == ""


async def test_distinct_builtin_is_a_conflict_left_untouched(
    mock: MockAnytype, client: AnytypeClient
) -> None:
    object_id = _seed(
        mock, "Mira", legacy="Old bot line.", builtin="Human's better line."
    )
    assert await migrate_script.migrate(client, dry_run=False) == (0, 0, 1)
    texts = _texts(await client.get_object(object_id))
    assert texts["description"] == "Human's better line."
    assert texts["gc_summary"] == "Old bot line."  # kept for the human


async def test_dry_run_writes_nothing(
    mock: MockAnytype, client: AnytypeClient
) -> None:
    object_id = _seed(mock, "Mira", legacy="Line.")
    assert await migrate_script.migrate(client, dry_run=True) == (1, 0, 0)
    texts = _texts(await client.get_object(object_id))
    assert texts["gc_summary"] == "Line."
    assert "description" not in texts
