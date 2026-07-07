"""WP14 space bindings: spaces.toml parsing and validation (ADR 019)."""

from __future__ import annotations

from pathlib import Path

import pytest

from graph_context.errors import GraphContextError
from graph_context.orchestrator.spaces import load_space_bindings

SPACE = "bafyreialpha.2lc"


def _write(tmp_path: Path, text: str) -> str:
    path = tmp_path / "spaces.toml"
    path.write_text(text)
    return str(path)


class TestLoadSpaceBindings:
    def test_a_minimal_binding_gets_the_default_profile(self, tmp_path: Path) -> None:
        path = _write(tmp_path, f'[spaces."{SPACE}"]\n')
        (binding,) = load_space_bindings(path, default_profile=None)
        assert binding.space_id == SPACE
        assert binding.profile.name == "fiction"
        assert binding.chat_id is None and binding.project is None

    def test_all_keys_are_kept_verbatim_when_given(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            f'[spaces."{SPACE}"]\n'
            'profile = "assistant"\nproject = "Todos"\n'
            'modes_file = "todo.toml"\nchat_id = "bafychat"\n',
        )
        (binding,) = load_space_bindings(path, default_profile="fiction")
        assert binding.profile.name == "assistant"
        assert binding.project == "Todos"
        assert binding.modes_file == "todo.toml"
        assert binding.chat_id == "bafychat"

    def test_unknown_keys_fail_naming_the_space_table(self, tmp_path: Path) -> None:
        path = _write(tmp_path, f'[spaces."{SPACE}"]\nspace_id = "x"\n')
        with pytest.raises(GraphContextError, match=SPACE) as excinfo:
            load_space_bindings(path, None)
        assert "space_id" in str(excinfo.value)  # channels.toml key, not ours

    def test_unknown_profile_fails_naming_the_space_table(
        self, tmp_path: Path
    ) -> None:
        path = _write(tmp_path, f'[spaces."{SPACE}"]\nprofile = "nope"\n')
        with pytest.raises(GraphContextError, match="nope"):
            load_space_bindings(path, None)

    def test_missing_file_and_bad_toml_fail_loudly_with_the_path(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(GraphContextError, match="cannot read"):
            load_space_bindings(str(tmp_path / "absent.toml"), None)
        path = _write(tmp_path, "not [valid toml")
        with pytest.raises(GraphContextError, match="not valid TOML"):
            load_space_bindings(path, None)

    def test_an_empty_file_demands_at_least_one_table(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "# nothing bound\n")
        with pytest.raises(GraphContextError, match="at least one"):
            load_space_bindings(path, None)

    def test_the_table_key_is_the_space_id_so_duplicates_cannot_exist(
        self, tmp_path: Path
    ) -> None:
        # TOML itself rejects a duplicated table name -- the one-binding-
        # per-space invariant is structural, surfaced as a parse error.
        path = _write(
            tmp_path, f'[spaces."{SPACE}"]\n[spaces."{SPACE}"]\n'
        )
        with pytest.raises(GraphContextError, match="not valid TOML"):
            load_space_bindings(path, None)
