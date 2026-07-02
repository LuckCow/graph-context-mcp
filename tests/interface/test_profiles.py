"""Domain profiles (WP5): framing changes, wire format never does.

The golden files under ``golden/`` pin each profile's assembled tool
docstrings. Docstrings are prompts -- a golden diff IS the review artifact
for a prompt change. Regenerate deliberately after editing profiles.py:

    GC_REGEN_GOLDENS=1 pytest tests/interface/test_profiles.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from graph_context.application.node_writer import NodeWriter
from graph_context.domain.models import NodeDraft
from graph_context.domain.schema import Role
from graph_context.domain.session import SessionState
from graph_context.errors import GraphContextError, SchemaViolation
from graph_context.infrastructure.memory.fake_repository import InMemoryGraphRepository
from graph_context.interface.profiles import (
    FICTION,
    PROFILES,
    TOOL_NAMES,
    WORKSPACE,
    DomainProfile,
    get_profile,
)

GOLDEN_DIR = Path(__file__).parent / "golden"


def _render(profile: DomainProfile) -> str:
    parts = [f"### {name}\n{profile.tool_docs[name]}" for name in TOOL_NAMES]
    return "\n".join(parts)


class TestProfileSelection:
    def test_default_is_fiction(self) -> None:
        assert get_profile(None) is FICTION
        assert get_profile("") is FICTION

    def test_names_resolve_case_insensitively(self) -> None:
        assert get_profile("Workspace") is WORKSPACE

    def test_unknown_profile_error_lists_allowed_values(self) -> None:
        with pytest.raises(GraphContextError) as err:
            get_profile("enterprise")
        assert "fiction" in str(err.value) and "workspace" in str(err.value)

    def test_every_profile_documents_every_tool(self) -> None:
        for profile in PROFILES.values():
            assert set(profile.tool_docs) == set(TOOL_NAMES)
            assert all(doc.strip() for doc in profile.tool_docs.values())

    def test_incomplete_profile_is_unrepresentable(self) -> None:
        with pytest.raises(ValueError, match="tool_docs mismatch"):
            DomainProfile(
                name="broken", description="", tool_docs={}, role_overrides={}
            )


class TestProfileFraming:
    """The two profiles genuinely diverge where the domain shows."""

    @pytest.mark.parametrize("profile", [FICTION, WORKSPACE], ids=lambda p: p.name)
    def test_docstrings_match_golden(self, profile: DomainProfile) -> None:
        path = GOLDEN_DIR / f"profile_{profile.name}.txt"
        rendered = _render(profile)
        if os.environ.get("GC_REGEN_GOLDENS"):
            path.write_text(rendered)
        assert rendered == path.read_text(), (
            "profile docstrings changed; if intentional, regenerate via "
            "GC_REGEN_GOLDENS=1 and review the golden diff as a prompt change"
        )

    def test_worked_examples_speak_the_profile_domain(self) -> None:
        assert "SCENE ASSEMBLY" in FICTION.tool_docs["explore"]
        assert "Character" in FICTION.tool_docs["create_node"]
        assert "MEETING or DECISION BRIEF" in WORKSPACE.tool_docs["explore"]
        assert "Meeting" in WORKSPACE.tool_docs["create_node"]

    def test_domain_neutral_docs_are_shared_not_forked(self) -> None:
        for name in ("update_node", "find_node"):
            assert FICTION.tool_docs[name] is WORKSPACE.tool_docs[name]


class TestWorkspaceRoleOverrides:
    def test_meetings_and_decisions_carry_the_event_role(self) -> None:
        for key in ("meeting", "decision", "milestone"):
            assert WORKSPACE.role_overrides[key] is Role.EVENT

    async def test_event_role_override_enforces_the_time_invariant(self) -> None:
        """The one behavioral consequence of a role override: Event-role
        nodes require a timeline position at creation (schema invariant,
        applied by NodeWriter through repo.role_for)."""
        repo = InMemoryGraphRepository(role_overrides=WORKSPACE.role_overrides)
        writer = NodeWriter(repo, SessionState())
        with pytest.raises(SchemaViolation, match="story_time"):
            await writer.create_node(
                NodeDraft("Meeting", name="Standup", summary="Daily sync.")
            )
        node = await writer.create_node(
            NodeDraft(
                "Meeting", name="Standup", summary="Daily sync.",
                story_time=20260702,
            )
        )
        assert node.role is Role.EVENT


class TestServerRegistration:
    async def test_registered_descriptions_come_from_the_active_profile(self) -> None:
        from graph_context.interface import server

        expected = get_profile(os.environ.get("GC_PROFILE"))
        listed = {tool.name: tool.description for tool in await server.mcp.list_tools()}
        assert set(listed) == set(TOOL_NAMES)
        for name in TOOL_NAMES:
            assert listed[name] == expected.tool_docs[name]
