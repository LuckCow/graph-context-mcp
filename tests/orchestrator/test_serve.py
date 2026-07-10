"""The consolidated server: gates, and which transports actually launch.

The bots themselves are stubbed -- their own suites pin their behavior;
here the contract is serve's: the Discord gate (compose sets the token
env var unconditionally, so file CONTENT decides), the viewer knobs,
and fail-together crash semantics.
"""

from __future__ import annotations

import pytest

from graph_context.errors import GraphContextError
from graph_context.orchestrator import anytype_chat_bot, discord_bot, serve


@pytest.fixture
def token_file(tmp_path, monkeypatch):
    """A non-empty token secret, wired into the env."""
    token = tmp_path / "token"
    token.write_text("secret-token\n")
    monkeypatch.setenv("DISCORD_BOT_TOKEN_FILE", str(token))
    return token


@pytest.fixture
def one_channel_bound(tmp_path, monkeypatch):
    """A channels file binding one channel, wired into the env."""
    channels = tmp_path / "channels.toml"
    channels.write_text('[channels.42]\nspace_id = "bafy-test"\n')
    monkeypatch.setenv("GC_CHANNELS_FILE", str(channels))
    monkeypatch.delenv("GC_DISCORD_CHANNELS", raising=False)


class TestDiscordConfiguredGate:
    def test_an_unset_token_file_env_means_not_configured(
        self, monkeypatch, one_channel_bound
    ) -> None:
        monkeypatch.delenv("DISCORD_BOT_TOKEN_FILE", raising=False)
        assert discord_bot.is_configured() is False

    def test_an_empty_token_file_means_not_configured(
        self, tmp_path, monkeypatch, one_channel_bound
    ) -> None:
        # A sanctioned "no Discord" state: compose mounts the secret
        # unconditionally, so emptying the file is the off switch.
        token = tmp_path / "token"
        token.write_text("\n")
        monkeypatch.setenv("DISCORD_BOT_TOKEN_FILE", str(token))
        assert discord_bot.is_configured() is False

    def test_a_token_with_a_bound_channel_means_configured(
        self, token_file, one_channel_bound
    ) -> None:
        assert discord_bot.is_configured() is True

    def test_a_zero_table_channels_file_means_parked(
        self, tmp_path, monkeypatch, token_file
    ) -> None:
        # The other sanctioned "no Discord" state: the WP14 cutover left
        # the token in place but commented every channel binding out.
        channels = tmp_path / "channels.toml"
        channels.write_text("# all bindings moved to spaces.toml\n")
        monkeypatch.setenv("GC_CHANNELS_FILE", str(channels))
        monkeypatch.delenv("GC_DISCORD_CHANNELS", raising=False)
        assert discord_bot.is_configured() is False

    def test_a_legacy_allowlist_means_configured(
        self, monkeypatch, token_file
    ) -> None:
        monkeypatch.delenv("GC_CHANNELS_FILE", raising=False)
        monkeypatch.setenv("GC_DISCORD_CHANNELS", "42")
        assert discord_bot.is_configured() is True

    def test_an_unreadable_channels_file_defers_to_the_loud_path(
        self, tmp_path, monkeypatch, token_file
    ) -> None:
        # The gate stays True so run() reaches load_channel_bindings,
        # whose error names the file -- the message lives in one place.
        monkeypatch.setenv("GC_CHANNELS_FILE", str(tmp_path / "gone.toml"))
        monkeypatch.delenv("GC_DISCORD_CHANNELS", raising=False)
        assert discord_bot.is_configured() is True

    def test_a_missing_token_file_fails_loudly(
        self, tmp_path, monkeypatch, one_channel_bound
    ) -> None:
        monkeypatch.setenv("DISCORD_BOT_TOKEN_FILE", str(tmp_path / "gone"))
        with pytest.raises(GraphContextError, match="cannot read the bot token"):
            discord_bot.is_configured()


@pytest.fixture
def no_viewer(monkeypatch):
    """Serve without the HTTP viewer -- these tests pin task wiring only."""
    monkeypatch.setenv("GC_TURN_LOG", "off")


@pytest.fixture
def launched(monkeypatch):
    """Stub both bots with recording no-op coroutines."""
    calls: list[str] = []

    def record(name: str):
        async def run() -> None:
            calls.append(name)
        return run

    monkeypatch.setattr(anytype_chat_bot, "run", record("anytype"))
    monkeypatch.setattr(discord_bot, "run", record("discord"))
    return calls


class TestLaunch:
    async def test_both_bots_launch_when_discord_is_configured(
        self, no_viewer, launched, token_file, one_channel_bound
    ) -> None:
        await serve.run()
        assert sorted(launched) == ["anytype", "discord"]

    async def test_discord_is_skipped_when_not_configured(
        self, monkeypatch, no_viewer, launched
    ) -> None:
        monkeypatch.delenv("DISCORD_BOT_TOKEN_FILE", raising=False)
        await serve.run()
        assert launched == ["anytype"]

    async def test_one_bot_crashing_takes_the_server_down(
        self, monkeypatch, no_viewer, launched
    ) -> None:
        async def crash() -> None:
            raise GraphContextError("GC_SPACES_FILE is unset")

        monkeypatch.setattr(anytype_chat_bot, "run", crash)
        monkeypatch.delenv("DISCORD_BOT_TOKEN_FILE", raising=False)
        with pytest.raises(BaseExceptionGroup) as err:
            await serve.run()
        assert err.group_contains(GraphContextError)

    async def test_the_viewer_is_skipped_when_the_diary_is_off(
        self, monkeypatch, no_viewer, launched, caplog
    ) -> None:
        monkeypatch.delenv("DISCORD_BOT_TOKEN_FILE", raising=False)
        with caplog.at_level("INFO", logger="graph_context.orchestrator.serve"):
            await serve.run()
        assert "GC_TURN_LOG is off" in caplog.text
