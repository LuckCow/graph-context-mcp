"""WP14 space bindings: spaces.toml parsing and validation (ADR 019)."""

from __future__ import annotations

from pathlib import Path

import pytest

from graph_context.errors import GraphContextError
from graph_context.interface.profiles import get_profile
from graph_context.orchestrator.spaces import (
    SpaceBinding,
    load_space_bindings,
    served_chat_ids,
)

SPACE = "bafyreialpha.2lc"
FICTION = get_profile("fiction")


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

    def test_exclude_chats_parses_as_a_string_list(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            f'[spaces."{SPACE}"]\nexclude_chats = ["chat-a", "chat-b"]\n',
        )
        (binding,) = load_space_bindings(path, "fiction")
        assert binding.exclude_chats == ("chat-a", "chat-b")

    def test_exclude_chats_rejects_a_non_list(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path, f'[spaces."{SPACE}"]\nexclude_chats = "chat-a"\n'
        )
        with pytest.raises(GraphContextError, match="list of non-empty"):
            load_space_bindings(path, "fiction")

    def test_chat_id_and_exclude_chats_are_mutually_exclusive(
        self, tmp_path: Path
    ) -> None:
        path = _write(
            tmp_path,
            f'[spaces."{SPACE}"]\nchat_id = "pin"\nexclude_chats = ["x"]\n',
        )
        with pytest.raises(GraphContextError, match="mutually exclusive"):
            load_space_bindings(path, "fiction")


class TestServedChatIds:
    def _binding(self, **kw: object) -> SpaceBinding:
        return SpaceBinding(space_id=SPACE, profile=FICTION, **kw)  # type: ignore[arg-type]

    def test_serves_all_listed_chats_by_default(self) -> None:
        assert served_chat_ids(self._binding(), ["a", "b", "c"]) == ("a", "b", "c")

    def test_a_pin_serves_only_itself_ignoring_the_list(self) -> None:
        assert served_chat_ids(self._binding(chat_id="pin"), ["a", "b"]) == ("pin",)

    def test_exclusions_are_removed_order_preserved(self) -> None:
        binding = self._binding(exclude_chats=("b",))
        assert served_chat_ids(binding, ["a", "b", "c"]) == ("a", "c")
