"""Seed-TOML parsing + the packaged starter corpora (ADR 035).

The seed TOML is startup config: every problem fails loudly naming the
file and table. The packaged sets are the retired profile mode specs'
successors -- the goal/binding/capture content was pinned equal to the
profile constants at the WP26 cutover (this file's round-trip tests are
what remains of that pin now the constants are gone).
"""

from __future__ import annotations

import pytest

from graph_context.errors import GraphContextError
from graph_context.interface import mode_config
from graph_context.interface.mode_config import (
    default_seed,
    load_seed_modes,
    parse_seed_modes,
    seed_payloads,
)
from graph_context.interface.profiles import CapturePolicy

GOOD = """
[modes.organizing]
default = true
icon = "X"
mutating = true
goal = "Maintain the knowledge base."

[modes.record_procedure]
goal = "Notate each step."
[modes.record_procedure.capture]
artifact_type = "procedure"
min_chars = 120
"""


class TestParseSeedModes:
    def test_a_good_corpus_parses_specs_and_dressing(self) -> None:
        organizing, procedure = parse_seed_modes(GOOD, "test")
        assert organizing.name == "organizing"
        assert organizing.display_name == "Organizing"
        assert organizing.icon == "X"
        assert organizing.default is True
        assert organizing.spec.mutating is True
        assert procedure.display_name == "Record Procedure"
        assert procedure.default is False
        assert procedure.spec.capture == CapturePolicy(
            artifact_type="procedure", min_chars=120
        )

    def test_first_table_is_the_default_when_none_marked(self) -> None:
        seeds = parse_seed_modes(
            '[modes.b_mode]\ngoal = "g"\n[modes.a_mode]\ngoal = "g"\n', "test"
        )
        assert default_seed(seeds).name == "b_mode"  # order, not alphabet

    def test_two_defaults_fail_loudly_naming_both(self) -> None:
        text = (
            '[modes.one]\ndefault = true\ngoal = "g"\n'
            '[modes.two]\ndefault = true\ngoal = "g"\n'
        )
        with pytest.raises(GraphContextError, match="one, two"):
            parse_seed_modes(text, "test")

    def test_bad_corpora_fail_loudly_naming_the_spot(self) -> None:
        with pytest.raises(GraphContextError, match="goal"):
            parse_seed_modes("[modes.broken]\nmutating = true\n", "test")
        with pytest.raises(GraphContextError, match="unknown keys"):
            parse_seed_modes('[modes.broken]\ngoal = "g"\nprompt = "x"\n', "test")
        with pytest.raises(GraphContextError, match="at least one"):
            parse_seed_modes("", "test")
        with pytest.raises(GraphContextError, match="not valid TOML"):
            parse_seed_modes("[modes", "test")
        with pytest.raises(GraphContextError, match="default must be a boolean"):
            parse_seed_modes('[modes.m]\ngoal = "g"\ndefault = "yes"\n', "test")

    def test_enum_typos_fail_naming_the_table_and_choices(self) -> None:
        with pytest.raises(GraphContextError) as err:
            parse_seed_modes(
                '[modes.loud]\ngoal = "g"\nactivity_detail = "verbose"\n',
                "seed",
            )
        assert "[modes.loud]" in str(err.value)
        assert "off, minimal, tools, full" in str(err.value)
        with pytest.raises(GraphContextError) as err:
            parse_seed_modes(
                '[modes.heavy]\ngoal = "g"\nmodel = "haiku 3"\n', "seed"
            )
        assert "[modes.heavy]" in str(err.value)
        assert "sonnet 5, opus 4.8, fable 5" in str(err.value)

    def test_goal_edges_are_stripped(self) -> None:
        (seed,) = parse_seed_modes('[modes.m]\ngoal = "  g  "\n', "test")
        assert seed.spec.goal == "g"


class TestLoadSeedModes:
    def test_an_explicit_source_wins_and_missing_files_fail(self, tmp_path) -> None:
        path = tmp_path / "custom.toml"
        path.write_text('[modes.custom]\ngoal = "g"\n')
        (seed,) = load_seed_modes(str(path), "fiction")
        assert seed.name == "custom"
        with pytest.raises(GraphContextError, match="cannot read"):
            load_seed_modes(str(tmp_path / "absent.toml"), "fiction")

    def test_unknown_profile_has_no_packaged_corpus(self) -> None:
        with pytest.raises(GraphContextError, match="nope"):
            load_seed_modes(None, "nope")


class TestPackagedCorpora:
    """The packaged sets replace the retired profile mode specs; these
    pins are what keeps their content honest (read via
    importlib.resources, so a packaging regression fails here too)."""

    def test_fiction_and_workspace_ship_the_modeling_pair(self) -> None:
        for profile in ("fiction", "workspace"):
            seeds = load_seed_modes(None, profile)
            by_name = {s.name: s for s in seeds}
            assert set(by_name) == {"world_modeling", "authoring"}
            assert default_seed(seeds).name == "world_modeling"
            assert by_name["world_modeling"].spec.mutating is True
            authoring = by_name["authoring"].spec
            assert authoring.mutating is False
            assert authoring.capture == CapturePolicy(artifact_type="gc_prose")

    def test_assistant_ships_the_organizing_trio(self) -> None:
        seeds = load_seed_modes(None, "assistant")
        by_name = {s.name: s for s in seeds}
        assert set(by_name) == {
            "organizing", "record_procedure", "meeting_notes",
        }
        assert default_seed(seeds).name == "organizing"
        assert by_name["organizing"].spec.mutating is True
        assert by_name["record_procedure"].spec.capture == CapturePolicy(
            artifact_type="procedure", min_chars=120
        )
        assert by_name["meeting_notes"].spec.capture == CapturePolicy(
            artifact_type="note", min_chars=120
        )

    def test_every_seed_carries_an_icon_and_a_nonempty_goal(self) -> None:
        for profile in ("fiction", "workspace", "assistant"):
            for seed in load_seed_modes(None, profile):
                assert seed.icon, f"{profile}:{seed.name} has no icon"
                assert len(seed.spec.goal) > 100  # a real prompt, not a stub

    def test_display_names_slugify_back_to_the_mode_name(self) -> None:
        for profile in ("fiction", "workspace", "assistant"):
            for seed in load_seed_modes(None, profile):
                assert mode_config.slugify(seed.display_name) == seed.name


class TestSeedPayloads:
    """The payload shape is the ModeStore port's: one representation
    feeds the memory store, the Anytype seeder, and the eval runner."""

    def test_payloads_mirror_the_store_port_shape(self) -> None:
        organizing, procedure = seed_payloads(parse_seed_modes(GOOD, "test"))
        assert organizing == {
            "id": "seed:organizing",
            "name": "Organizing",
            "goal": "Maintain the knowledge base.",
            "mutating": True,
            "web_search": False,
            "capture": None,
            "activity_detail": "minimal",
            "origin": "seed [modes.organizing]",
            "icon": "X",
            "default": True,
        }
        assert procedure["capture"] == {
            "artifact_type": "procedure",
            "references_label": "references",
            "min_chars": 120,
        }
        assert "model" not in procedure  # unset pins nothing
