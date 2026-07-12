"""``build_driver``: GC_DRIVER resolution and its fail-loud edges.

The anthropic branch's billing gate is the load-bearing behavior here:
``GC_DRIVER=anthropic_api`` without a key must refuse to start, because the
credit-vs-subscription switch has to be a conscious choice, never an SDK
fallback quietly picking a payer.
"""

from __future__ import annotations

import pytest

from graph_context.errors import GraphContextError
from graph_context.orchestrator.bootstrap import build_driver
from graph_context.orchestrator.drivers import LLMTurn, TranscriptEvent


class TestDriverSelection:
    def test_manual_needs_no_sdk(self, monkeypatch) -> None:
        monkeypatch.setenv("GC_DRIVER", "manual")
        driver, model, _help = build_driver()
        assert model == "manual"

    def test_unknown_driver_names_all_choices(self, monkeypatch) -> None:
        monkeypatch.setenv("GC_DRIVER", "gpt")
        with pytest.raises(
            GraphContextError,
            match="anthropic_subscription.*anthropic_api.*manual",
        ):
            build_driver()

    def test_bad_effort_fails_before_any_driver_import(self, monkeypatch) -> None:
        monkeypatch.setenv("GC_DRIVER", "anthropic_api")
        monkeypatch.setenv("GC_DRIVER_EFFORT", "turbo")
        with pytest.raises(GraphContextError, match="GC_DRIVER_EFFORT"):
            build_driver()

    @pytest.mark.parametrize("legacy", ["anthropic", "api"])
    def test_legacy_names_still_resolve_to_the_api_driver(
        self, monkeypatch, legacy
    ) -> None:
        # The vendor-namespaced values are new; env files carrying the old
        # SDK-flavored or payer-only names must keep working.
        monkeypatch.setenv("GC_DRIVER", legacy)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        # Resolving to the api branch means hitting its key gate (or its
        # missing-SDK gate before the rebuild) -- never "unknown GC_DRIVER".
        with pytest.raises(
            GraphContextError, match="API credits|anthropic SDK"
        ):
            build_driver()

    @pytest.mark.parametrize("legacy", ["claude", "subscription"])
    def test_legacy_names_still_resolve_to_the_subscription_driver(
        self, monkeypatch, legacy
    ) -> None:
        pytest.importorskip("claude_agent_sdk")
        from graph_context.orchestrator.claude_driver import ClaudeAgentDriver

        monkeypatch.setenv("GC_DRIVER", legacy)
        driver, _model, _help = build_driver()
        assert isinstance(driver, ClaudeAgentDriver)


class TestAnthropicApiBranch:
    def test_a_missing_key_refuses_to_start(self, monkeypatch) -> None:
        pytest.importorskip("anthropic")
        monkeypatch.setenv("GC_DRIVER", "anthropic_api")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        with pytest.raises(GraphContextError, match="API credits"):
            build_driver()

    def test_a_key_builds_the_driver_with_model_and_effort(
        self, monkeypatch
    ) -> None:
        pytest.importorskip("anthropic")
        from graph_context.orchestrator.anthropic_driver import AnthropicDriver

        monkeypatch.setenv("GC_DRIVER", "anthropic_api")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
        monkeypatch.setenv("GC_DRIVER_MODEL", "claude-opus-4-8")
        monkeypatch.setenv("GC_DRIVER_EFFORT", "low")
        driver, model, help_line = build_driver()
        assert isinstance(driver, AnthropicDriver)
        assert model == "claude-opus-4-8"
        assert "credits" in help_line

    def test_no_model_override_reports_the_driver_default(
        self, monkeypatch
    ) -> None:
        pytest.importorskip("anthropic")
        from graph_context.orchestrator.anthropic_driver import DEFAULT_MODEL

        monkeypatch.setenv("GC_DRIVER", "anthropic_api")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
        monkeypatch.delenv("GC_DRIVER_MODEL", raising=False)
        monkeypatch.delenv("GC_DRIVER_EFFORT", raising=False)
        _driver, model, _help = build_driver()
        assert model == DEFAULT_MODEL


class TestManualDriverStillDecides:
    async def test_the_manual_seam_still_satisfies_the_protocol(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("GC_DRIVER", "manual")
        driver, _model, _help = build_driver()
        turn = await driver.decide([TranscriptEvent("user", "hi")], {}, "")
        assert isinstance(turn, LLMTurn)
