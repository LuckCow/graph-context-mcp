"""Channel-bound spaces: the static channel -> space map (ADR 017).

Each Discord channel can be bound to its own Anytype space, with its own
profile, project label, and modes file, declared once at startup in a
``GC_CHANNELS_FILE`` TOML file::

    [channels.1523551542123298896]
    space_id   = "bafyre..."       # required
    profile    = "fiction"         # optional; defaults to GC_PROFILE
    project    = "Ashfall"         # optional cosmetic label
    modes_file = "ashfall.toml"    # optional; overrides GC_MODES_FILE

Like ``modes.py`` this stays plain logic over primitives -- no discord.py,
no infrastructure -- so bindings parse and validate without either. Bad
config fails LOUDLY at startup, naming the file, channel, and field.

One channel per space is an invariant, not a limitation we forgot. It is
no longer about the session node -- WP8/ADR 021 made sessions keyed, so
one space holds many session nodes. The remaining reason is that a
runtime owns a repository/GraphIndex and a journal: two runtimes on one
space would keep divergent graph projections and cross-attribute each
other's mutations. Multiple chats in one space share ONE runtime (the
Anytype transport); binding a space to two *channels* would not.
"""

from __future__ import annotations

import asyncio
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from graph_context.errors import GraphContextError
from graph_context.interface import profiles
from graph_context.interface.profiles import DomainProfile

if TYPE_CHECKING:
    from graph_context.orchestrator.pipeline import Orchestrator

_BINDING_KEYS = {"space_id", "profile", "project", "modes_file"}


@dataclass(frozen=True, slots=True)
class ChannelBinding:
    """One channel's declared runtime: which space, worded how."""

    channel_id: int
    space_id: str
    profile: DomainProfile
    project: str | None = None
    modes_file: str | None = None


@dataclass
class ChannelRoute:
    """A live runtime behind a channel, with its own turn lock.

    Turns against different spaces may interleave (separate repositories,
    sessions, journals, write queues; the driver is stateless), so the
    lock is per-route rather than process-wide; channels that share one
    legacy runtime share one route and therefore still serialize.
    """

    orchestrator: Orchestrator
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def channels_declared(path: str) -> bool:
    """Is at least one channel bound in ``GC_CHANNELS_FILE``?

    The consolidated server's dormancy probe: a channels file with ZERO
    tables means "Discord parked" (the WP14 cutover left exactly that),
    not a broken config. Unreadable/invalid files return True so the
    caller proceeds into :func:`load_channel_bindings`, which names the
    problem loudly -- the error message lives in one place.
    """
    try:
        data = tomllib.loads(Path(path).read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return True  # defer to load_channel_bindings' precise loud error
    channels = data.get("channels")
    return isinstance(channels, dict) and bool(channels)


def load_channel_bindings(
    path: str, default_profile: str | None
) -> tuple[ChannelBinding, ...]:
    """Parse and validate ``GC_CHANNELS_FILE``; every problem names its spot.

    ``default_profile`` is the raw ``GC_PROFILE`` value: entries without
    their own ``profile`` resolve through it (unset -> fiction, as
    everywhere else).
    """
    try:
        data = tomllib.loads(Path(path).read_text())
    except OSError as err:
        raise GraphContextError(f"cannot read GC_CHANNELS_FILE at {path}: {err}") from None
    except tomllib.TOMLDecodeError as err:
        raise GraphContextError(f"GC_CHANNELS_FILE is not valid TOML: {err}") from None
    channels = data.get("channels")
    if not isinstance(channels, dict) or not channels:
        raise GraphContextError(
            "GC_CHANNELS_FILE must define at least one [channels.<discord-id>] table"
        )
    bindings: list[ChannelBinding] = []
    spaces: dict[str, int] = {}  # space_id -> first channel bound to it
    for raw_id, body in channels.items():
        origin = f"GC_CHANNELS_FILE [channels.{raw_id}]"
        try:
            channel_id = int(raw_id)
        except ValueError:
            raise GraphContextError(
                f"{origin}: the table name must be a numeric Discord channel id"
            ) from None
        if not isinstance(body, dict):
            raise GraphContextError(f"{origin} must be a table")
        bindings.append(_binding_from_mapping(channel_id, body, origin, default_profile))
        space_id = bindings[-1].space_id
        if space_id in spaces:
            raise GraphContextError(
                f"channels {spaces[space_id]} and {channel_id} both bind space "
                f"{space_id}; a space holds one SessionContext node, so one "
                "channel per space (a keyed multi-session store is WP8)"
            )
        spaces[space_id] = channel_id
    return tuple(bindings)


def _binding_from_mapping(
    channel_id: int, body: dict[str, Any], origin: str, default_profile: str | None
) -> ChannelBinding:
    unknown = set(body) - _BINDING_KEYS
    if unknown:
        raise GraphContextError(
            f"{origin} has unknown keys {sorted(unknown)}; "
            f"allowed: {sorted(_BINDING_KEYS)}"
        )
    space_id = body.get("space_id")
    if not isinstance(space_id, str) or not space_id.strip():
        raise GraphContextError(f"{origin}: space_id is required and must be a non-empty string")
    for key in ("profile", "project", "modes_file"):
        value = body.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise GraphContextError(f"{origin}: {key} must be a non-empty string")
    try:
        profile = profiles.get_profile(body.get("profile") or default_profile)
    except GraphContextError as err:
        raise GraphContextError(f"{origin}: {err}") from None
    return ChannelBinding(
        channel_id=channel_id,
        space_id=space_id.strip(),
        profile=profile,
        project=body.get("project"),
        modes_file=body.get("modes_file"),
    )
