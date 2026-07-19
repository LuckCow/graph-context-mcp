"""Space-bound chats: the static space -> profile/chat map (WP14, ADR 019).

The Anytype-chat analogue of :mod:`graph_context.orchestrator.channels`
(deliberately a sibling, not a generalization: that module's validation
story is Discord-shaped -- numeric table names, allowlist wording). Each
served space is declared once at startup in a ``GC_SPACES_FILE`` TOML
file, keyed by the space id itself::

    [spaces."bafyre..."]
    profile       = "fiction"      # optional; defaults to GC_PROFILE
    project       = "Ashfall"      # optional cosmetic label
    modes_file    = "ashfall.toml" # optional; the SEED source (ADR 035)
                                    # for a space with no Activity Mode
                                    # objects; overrides GC_MODES_FILE
    chat_id       = "bafyre..."    # optional PIN: serve ONLY this chat,
                                    # no discovery (single-chat deployments)
    exclude_chats = ["bafyre..."]  # optional; chat ids the bot ignores

The mode NEW chats start in is NOT declared here (ADR 034, retiring
WP21's ``default_mode`` key): it lives in the space itself, on the Space
Context object's default-mode link, next to the Activity Mode objects it
points at.

By default the bot serves EVERY chat in the space (WP8): each chat is a
separate THREAD with its own session context (scratchpad / working set /
mode), so creating a chat in Anytype creates a new thread with no config
change. ``exclude_chats`` opts specific chats out; ``chat_id`` pins to a
single chat and disables discovery (they are mutually exclusive). One
binding per space is structural (the table key IS the space id, so TOML
rejects duplicates); per-chat sessions are keyed nodes (WP8, ADR 021).
Like ``channels.py`` this stays plain logic over primitives -- no httpx,
no infrastructure -- and bad config fails LOUDLY at startup, naming the
file, space, and field.
"""

from __future__ import annotations

import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from graph_context.errors import GraphContextError
from graph_context.interface import profiles
from graph_context.interface.profiles import DomainProfile

_BINDING_KEYS = {
    "profile", "project", "modes_file", "chat_id", "exclude_chats",
}


@dataclass(frozen=True, slots=True)
class SpaceBinding:
    """One served space's declared runtime and which chats to listen on."""

    space_id: str
    profile: DomainProfile
    project: str | None = None
    modes_file: str | None = None
    chat_id: str | None = None  # pin: serve only this chat (no discovery)
    exclude_chats: tuple[str, ...] = ()


def served_chat_ids(
    binding: SpaceBinding, listed: Sequence[str]
) -> tuple[str, ...]:
    """Which of the space's chats this binding serves (WP8).

    A pinned ``chat_id`` is served verbatim (its presence in the space is
    the transport's concern, not this pure policy's). Otherwise every
    listed chat except those in ``exclude_chats``, order preserved.
    """
    if binding.chat_id:
        return (binding.chat_id,)
    excluded = frozenset(binding.exclude_chats)
    return tuple(cid for cid in listed if cid not in excluded)


def load_space_bindings(
    path: str, default_profile: str | None
) -> tuple[SpaceBinding, ...]:
    """Parse and validate ``GC_SPACES_FILE``; every problem names its spot.

    ``default_profile`` is the raw ``GC_PROFILE`` value: entries without
    their own ``profile`` resolve through it (unset -> fiction, as
    everywhere else).
    """
    try:
        data = tomllib.loads(Path(path).read_text())
    except OSError as err:
        raise GraphContextError(f"cannot read GC_SPACES_FILE at {path}: {err}") from None
    except tomllib.TOMLDecodeError as err:
        raise GraphContextError(f"GC_SPACES_FILE is not valid TOML: {err}") from None
    spaces = data.get("spaces")
    if not isinstance(spaces, dict) or not spaces:
        raise GraphContextError(
            'GC_SPACES_FILE must define at least one [spaces."<space-id>"] table'
        )
    bindings = []
    for space_id, body in spaces.items():
        origin = f'GC_SPACES_FILE [spaces."{space_id}"]'
        if not space_id.strip():
            raise GraphContextError(
                f"{origin}: the table name must be a non-empty Anytype space id"
            )
        if not isinstance(body, dict):
            raise GraphContextError(f"{origin} must be a table")
        bindings.append(
            _binding_from_mapping(space_id.strip(), body, origin, default_profile)
        )
    return tuple(bindings)


def _binding_from_mapping(
    space_id: str, body: dict[str, Any], origin: str, default_profile: str | None
) -> SpaceBinding:
    if "default_mode" in body:
        raise GraphContextError(
            f"{origin}: default_mode moved into the space itself (ADR 034) "
            "-- link the Activity Mode object on the space's Space Context "
            "object instead, and remove this key"
        )
    unknown = set(body) - _BINDING_KEYS
    if unknown:
        raise GraphContextError(
            f"{origin} has unknown keys {sorted(unknown)}; "
            f"allowed: {sorted(_BINDING_KEYS)}"
        )
    for key in _BINDING_KEYS - {"exclude_chats"}:
        value = body.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise GraphContextError(f"{origin}: {key} must be a non-empty string")
    exclude_chats = _string_list(body.get("exclude_chats"), origin, "exclude_chats")
    if body.get("chat_id") and exclude_chats:
        raise GraphContextError(
            f"{origin}: chat_id (a single-chat pin) and exclude_chats "
            "(discover-all-but) are mutually exclusive -- set one, not both"
        )
    try:
        profile = profiles.get_profile(body.get("profile") or default_profile)
    except GraphContextError as err:
        raise GraphContextError(f"{origin}: {err}") from None
    return SpaceBinding(
        space_id=space_id,
        profile=profile,
        project=body.get("project"),
        modes_file=body.get("modes_file"),
        chat_id=body.get("chat_id"),
        exclude_chats=exclude_chats,
    )


def _string_list(value: Any, origin: str, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise GraphContextError(
            f"{origin}: {key} must be a list of non-empty chat-id strings"
        )
    return tuple(value)
