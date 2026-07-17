"""The memory backend is a freshly seeded space (ADR 035).

``composition.build_runtime`` pre-fills the in-memory mode and
space-context stores with the seed corpus, so the dev/CLI path loads the
same registry a healed Anytype space would -- default link included.
"""

from __future__ import annotations

import pytest

from graph_context import composition
from graph_context.errors import GraphContextError
from graph_context.interface.profiles import get_profile
from graph_context.orchestrator.modes import load_registry


@pytest.fixture(autouse=True)
def memory_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GC_BACKEND", "memory")


async def _registry_of(built: composition.BuiltRuntime):
    return load_registry(
        in_space=await built.mode_store.load(),
        space_context=await built.space_context_store.load(),
    )


async def test_the_memory_runtime_serves_the_packaged_corpus() -> None:
    built = await composition.build_runtime(get_profile("fiction"))
    registry = await _registry_of(built)
    assert registry.names() == ["authoring", "world_modeling"]
    assert registry.default == "world_modeling"
    await composition.run_teardown(built.teardown)


async def test_each_profile_seeds_its_own_corpus_and_default() -> None:
    built = await composition.build_runtime(get_profile("assistant"))
    registry = await _registry_of(built)
    assert registry.names() == [
        "meeting_notes", "organizing", "record_procedure",
    ]
    assert registry.default == "organizing"  # the marked seed, linked
    await composition.run_teardown(built.teardown)


async def test_a_modes_file_replaces_the_packaged_corpus(tmp_path) -> None:
    path = tmp_path / "custom.toml"
    path.write_text(
        '[modes.scribe]\ndefault = true\nmutating = true\ngoal = "Write."\n'
        '[modes.reader]\ngoal = "Read."\n'
    )
    built = await composition.build_runtime(
        get_profile("fiction"), modes_file=str(path)
    )
    registry = await _registry_of(built)
    assert registry.names() == ["reader", "scribe"]
    assert registry.default == "scribe"
    await composition.run_teardown(built.teardown)


async def test_a_broken_seed_corpus_fails_startup_loudly(tmp_path) -> None:
    path = tmp_path / "broken.toml"
    path.write_text("[modes.broken]\nmutating = true\n")  # no goal
    with pytest.raises(GraphContextError, match="broken"):
        await composition.build_runtime(
            get_profile("fiction"), modes_file=str(path)
        )
