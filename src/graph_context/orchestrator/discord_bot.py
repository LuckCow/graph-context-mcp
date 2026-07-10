"""The Discord transport adapter: composition root + client shim (WP8).

Discord connects OUTWARD via the Gateway websocket, so the bot runs
inside the firewalled devcontainer (egress rules in
``.devcontainer/init-firewall.sh``). This module stays a shim: every
per-message decision -- channel gate, ``discord:<id>`` identity,
chunking -- is plain logic in ``discord_transport``; here we only read
config, wire the runtime, and translate ``discord.Message`` into
``InboundMessage``. Only this module imports discord.py
(import-linter-enforced), mirroring how ``claude_driver`` quarantines
its framework.

Config (composed in .devcontainer/docker-compose.yml):
  DISCORD_BOT_TOKEN_FILE  file holding the bot token -- a file, not an
                          env var, for the same leak reasons as the
                          Anytype key
  GC_CHANNELS_FILE        channel->space bindings TOML (ADR 017): each
                          channel gets its own space, profile, project
                          label, and modes file
  GC_DISCORD_CHANNELS     legacy allowlist: channel id(s) served by ONE
                          env-configured runtime; mutually exclusive
                          with GC_CHANNELS_FILE. Unmapped channels are
                          ignored, not refused, in both forms

The Discord-side prerequisite is the MESSAGE CONTENT privileged intent
(developer portal -> Bot -> intents); without it every guild message
arrives with empty text and the gate logs a warning.

Run:  python -m graph_context.orchestrator.discord_bot
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from graph_context import composition
from graph_context.errors import GraphContextError
from graph_context.orchestrator import bootstrap
from graph_context.orchestrator.channels import channels_declared
from graph_context.orchestrator.discord_transport import (
    DiscordTurnHandler,
    InboundMessage,
)

logger = logging.getLogger(__name__)


def _token_from(path: str) -> str:
    """The token file's stripped content; an unreadable file fails loudly."""
    try:
        return Path(path).read_text().strip()
    except OSError as err:
        raise GraphContextError(f"cannot read the bot token from {path}: {err}") from err


def _read_token() -> str:
    path = os.environ.get("DISCORD_BOT_TOKEN_FILE", "").strip()
    if not path:
        raise GraphContextError(
            "DISCORD_BOT_TOKEN_FILE is unset; point it at the bot-token file "
            "(the devcontainer mounts /run/secrets/discord_bot_token)"
        )
    token = _token_from(path)
    if not token:
        raise GraphContextError(f"bot token file {path} is empty")
    return token


def is_configured() -> bool:
    """The consolidated server's gate: a token AND at least one channel bound.

    The devcontainer composes DISCORD_BOT_TOKEN_FILE and GC_CHANNELS_FILE
    unconditionally (and the secret file must exist for compose to start),
    so the parked states are an EMPTY token file or a channels file with
    zero tables. Anything past this gate fails loudly through ``run()``'s
    existing paths: an unreadable token file (a path was pointed at),
    invalid channel TOML, both channel shapes set, a token with NO channel
    shape at all.
    """
    token_path = os.environ.get("DISCORD_BOT_TOKEN_FILE", "").strip()
    if not token_path or not _token_from(token_path):
        return False
    if os.environ.get("GC_DISCORD_CHANNELS", "").strip():
        return True  # legacy allowlist declares channels inline
    channels_file = os.environ.get("GC_CHANNELS_FILE", "").strip()
    if not channels_file:
        return True  # token without any channel shape: fail loudly in run()
    return channels_declared(channels_file)


async def run() -> None:
    """Serve the bound Discord channels until cancelled.

    Loop-composable: no logging setup, teardown in ``finally`` -- the
    consolidated server (``serve``) runs this next to the other
    transports; ``main()`` wraps it for standalone launches.
    """
    try:
        import discord
    except ImportError as err:
        raise GraphContextError(
            "the Discord transport needs discord.py (a container rebuild "
            "installs the [bot] extra)"
        ) from err

    token = _read_token()
    runtimes = await bootstrap.build_channel_runtimes()
    handler = DiscordTurnHandler(routes=runtimes.routes)

    intents = discord.Intents.default()
    intents.message_content = True  # ALSO needs enabling in the dev portal

    client = discord.Client(intents=intents)

    async def on_ready() -> None:
        served = "; ".join(
            f"{cid}: {desc}" for cid, desc in sorted(runtimes.descriptions.items())
        )
        logger.info(
            "discord: logged in as %s; serving %s. %s",
            client.user, served, runtimes.help_line,
        )

    async def on_message(message: discord.Message) -> None:
        inbound = InboundMessage(
            channel_id=message.channel.id,
            author_id=message.author.id,
            author_is_bot=message.author.bot,
            content=message.content,
        )
        if not handler.accepts(inbound):
            return
        try:
            async with message.channel.typing():
                await handler.run_turn(inbound, message.channel.send)
        except GraphContextError as err:
            # Config-shaped errors are actionable; show them in-channel.
            await message.channel.send(f"[error] {err}")
        except Exception:
            logger.exception("turn failed (channel=%s)", message.channel.id)
            await message.channel.send(
                "[error] the turn failed; see the bot log for the traceback"
            )

    # Plain registration instead of @client.event: the decorator is untyped
    # until discord.py is installed (CI runs mypy without the [bot] extra).
    client.event(on_ready)
    client.event(on_message)

    try:
        async with client:
            await client.start(token)
    finally:
        await composition.run_teardown(runtimes.teardown)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    await run()


if __name__ == "__main__":
    asyncio.run(main())
