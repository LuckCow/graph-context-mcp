"""ADR 017 channel-bound spaces: the bindings file and the multi-runtime
bootstrap.

Parsing is plain logic (no infrastructure); the bootstrap tests run over
``GC_BACKEND=memory`` with the manual driver, so each channel's runtime
is a real, independent ``Services`` bundle without any Anytype server.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from graph_context.errors import GraphContextError
from graph_context.orchestrator import bootstrap
from graph_context.orchestrator.channels import load_channel_bindings

CHANNEL_A = 1523551542123298896
CHANNEL_B = 1523551542123298897


def _write(tmp_path: Path, text: str) -> str:
    path = tmp_path / "channels.toml"
    path.write_text(text)
    return str(path)


class TestChannelBindingsFile:
    def test_a_minimal_entry_binds_channel_to_space_with_the_default_profile(
        self, tmp_path: Path
    ) -> None:
        path = _write(tmp_path, f'[channels.{CHANNEL_A}]\nspace_id = "space-a"\n')
        (binding,) = load_channel_bindings(path, default_profile=None)
        assert binding.channel_id == CHANNEL_A
        assert binding.space_id == "space-a"
        assert binding.profile.name == "fiction"  # unset GC_PROFILE default
        assert binding.project is None
        assert binding.modes_file is None

    def test_per_channel_profile_project_and_modes_file_are_honored(
        self, tmp_path: Path
    ) -> None:
        path = _write(
            tmp_path,
            f"""
            [channels.{CHANNEL_A}]
            space_id = "space-a"
            profile = "assistant"
            project = "Fieldwork"
            modes_file = "fieldwork-modes.toml"
            """,
        )
        (binding,) = load_channel_bindings(path, default_profile="fiction")
        assert binding.profile.name == "assistant"
        assert binding.project == "Fieldwork"
        assert binding.modes_file == "fieldwork-modes.toml"

    def test_the_gc_profile_default_applies_to_entries_without_their_own(
        self, tmp_path: Path
    ) -> None:
        path = _write(tmp_path, f'[channels.{CHANNEL_A}]\nspace_id = "space-a"\n')
        (binding,) = load_channel_bindings(path, default_profile="workspace")
        assert binding.profile.name == "workspace"

    def test_missing_space_id_fails_loudly_naming_the_channel(
        self, tmp_path: Path
    ) -> None:
        path = _write(tmp_path, f'[channels.{CHANNEL_A}]\nprofile = "fiction"\n')
        with pytest.raises(GraphContextError, match=f"{CHANNEL_A}.*space_id"):
            load_channel_bindings(path, default_profile=None)

    def test_unknown_profile_fails_loudly_listing_allowed_values(
        self, tmp_path: Path
    ) -> None:
        path = _write(
            tmp_path,
            f'[channels.{CHANNEL_A}]\nspace_id = "s"\nprofile = "novelist"\n',
        )
        with pytest.raises(GraphContextError, match="allowed"):
            load_channel_bindings(path, default_profile=None)

    def test_two_channels_on_one_space_are_rejected(self, tmp_path: Path) -> None:
        """One SessionContext node per space: two runtimes would clobber
        each other's session snapshot."""
        path = _write(
            tmp_path,
            f'[channels.{CHANNEL_A}]\nspace_id = "shared"\n'
            f'[channels.{CHANNEL_B}]\nspace_id = "shared"\n',
        )
        with pytest.raises(GraphContextError, match="both bind space"):
            load_channel_bindings(path, default_profile=None)

    def test_non_numeric_channel_key_fails_loudly(self, tmp_path: Path) -> None:
        path = _write(tmp_path, '[channels.general]\nspace_id = "s"\n')
        with pytest.raises(GraphContextError, match="numeric"):
            load_channel_bindings(path, default_profile=None)

    def test_unknown_keys_in_an_entry_fail_loudly(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path, f'[channels.{CHANNEL_A}]\nspace_id = "s"\nspce = "typo"\n'
        )
        with pytest.raises(GraphContextError, match="unknown keys"):
            load_channel_bindings(path, default_profile=None)

    def test_missing_file_fails_loudly(self, tmp_path: Path) -> None:
        with pytest.raises(GraphContextError, match="GC_CHANNELS_FILE"):
            load_channel_bindings(str(tmp_path / "absent.toml"), default_profile=None)

    def test_an_empty_file_fails_loudly(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "")
        with pytest.raises(GraphContextError, match="at least one"):
            load_channel_bindings(path, default_profile=None)


@pytest.fixture
def _memory_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GC_BACKEND", "memory")
    monkeypatch.setenv("GC_DRIVER", "manual")
    monkeypatch.setenv("GC_TURN_LOG", "off")
    monkeypatch.delenv("GC_DISCORD_CHANNELS", raising=False)
    monkeypatch.delenv("GC_CHANNELS_FILE", raising=False)
    monkeypatch.delenv("GC_PROFILE", raising=False)
    monkeypatch.delenv("GC_MODES_FILE", raising=False)


@pytest.mark.usefixtures("_memory_env")
class TestBuildChannelRuntimes:
    async def test_each_channel_gets_its_own_orchestrator_and_services(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(
            "GC_CHANNELS_FILE",
            _write(
                tmp_path,
                f'[channels.{CHANNEL_A}]\nspace_id = "space-a"\n'
                f'[channels.{CHANNEL_B}]\nspace_id = "space-b"\nproject = "Field"\n',
            ),
        )
        runtimes = await bootstrap.build_channel_runtimes()
        first = runtimes.routes[CHANNEL_A]
        second = runtimes.routes[CHANNEL_B]
        assert first is not second
        assert first.orchestrator is not second.orchestrator
        assert first.orchestrator.services is not second.orchestrator.services
        assert first.lock is not second.lock
        assert second.orchestrator.services.session.project == "Field"

    async def test_per_channel_profile_selects_that_channels_mode_registry(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(
            "GC_CHANNELS_FILE",
            _write(
                tmp_path,
                f'[channels.{CHANNEL_A}]\nspace_id = "a"\nprofile = "fiction"\n'
                f'[channels.{CHANNEL_B}]\nspace_id = "b"\nprofile = "assistant"\n',
            ),
        )
        runtimes = await bootstrap.build_channel_runtimes()
        assert runtimes.routes[CHANNEL_A].orchestrator.registry.default == (
            "world_modeling"
        )
        assert runtimes.routes[CHANNEL_B].orchestrator.registry.default == (
            "organizing"
        )

    async def test_a_per_channel_modes_file_overrides_only_that_channel(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        modes_path = tmp_path / "field-modes.toml"
        modes_path.write_text(
            '[modes.surveying]\ngoal = "Walk the site and note everything."\n'
        )
        monkeypatch.setenv(
            "GC_CHANNELS_FILE",
            _write(
                tmp_path,
                f'[channels.{CHANNEL_A}]\nspace_id = "a"\n'
                f'[channels.{CHANNEL_B}]\nspace_id = "b"\n'
                f'modes_file = "{modes_path}"\n',
            ),
        )
        runtimes = await bootstrap.build_channel_runtimes()
        assert "surveying" not in runtimes.routes[CHANNEL_A].orchestrator.registry.names()
        assert "surveying" in runtimes.routes[CHANNEL_B].orchestrator.registry.names()

    async def test_legacy_allowlist_channels_share_one_runtime_and_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GC_DISCORD_CHANNELS", f"{CHANNEL_A},{CHANNEL_B}")
        runtimes = await bootstrap.build_channel_runtimes()
        assert runtimes.routes[CHANNEL_A] is runtimes.routes[CHANNEL_B]

    async def test_channels_file_and_allowlist_together_fail_loudly(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(
            "GC_CHANNELS_FILE",
            _write(tmp_path, f'[channels.{CHANNEL_A}]\nspace_id = "a"\n'),
        )
        monkeypatch.setenv("GC_DISCORD_CHANNELS", str(CHANNEL_B))
        with pytest.raises(GraphContextError, match="unset one"):
            await bootstrap.build_channel_runtimes()

    async def test_a_channel_that_fails_to_start_names_itself_in_the_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Fail-fast: a bad per-channel modes file stops the whole bot,
        with the channel and space in the message."""
        monkeypatch.setenv(
            "GC_CHANNELS_FILE",
            _write(
                tmp_path,
                f'[channels.{CHANNEL_A}]\nspace_id = "a"\n'
                f'[channels.{CHANNEL_B}]\nspace_id = "b"\n'
                'modes_file = "missing-modes.toml"\n',
            ),
        )
        with pytest.raises(GraphContextError, match=f"channel {CHANNEL_B} .space b."):
            await bootstrap.build_channel_runtimes()
