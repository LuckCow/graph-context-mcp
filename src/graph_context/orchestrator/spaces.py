"""Space-bound chats: the static space -> profile/chat map (WP14, ADR 019).

The Anytype-chat analogue of :mod:`graph_context.orchestrator.channels`
(deliberately a sibling, not a generalization: that module's validation
story is Discord-shaped -- numeric table names, allowlist wording). Each
served space is declared once at startup in a ``GC_SPACES_FILE`` TOML
file, keyed by the space id itself::

    [spaces."bafyre..."]
    profile    = "fiction"      # optional; defaults to GC_PROFILE
    project    = "Ashfall"      # optional cosmetic label
    modes_file = "ashfall.toml" # optional; overrides GC_MODES_FILE
    chat_id    = "bafyre..."    # optional; unset = discover at startup
                                 # (fails loudly unless the space has
                                 # exactly one chat)

Because the chat lives INSIDE the space, the table key IS the space id --
the one-binding-per-space invariant (one SessionContext node per space)
is structural here: TOML rejects duplicate table names, so no cross-check
is needed. Like ``channels.py`` this stays plain logic over primitives --
no httpx, no infrastructure -- and bad config fails LOUDLY at startup,
naming the file, space, and field.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from graph_context.errors import GraphContextError
from graph_context.interface import profiles
from graph_context.interface.profiles import DomainProfile

_BINDING_KEYS = {"profile", "project", "modes_file", "chat_id"}


@dataclass(frozen=True, slots=True)
class SpaceBinding:
    """One served space's declared runtime, plus which chat to listen on."""

    space_id: str
    profile: DomainProfile
    project: str | None = None
    modes_file: str | None = None
    chat_id: str | None = None


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
    unknown = set(body) - _BINDING_KEYS
    if unknown:
        raise GraphContextError(
            f"{origin} has unknown keys {sorted(unknown)}; "
            f"allowed: {sorted(_BINDING_KEYS)}"
        )
    for key in _BINDING_KEYS:
        value = body.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise GraphContextError(f"{origin}: {key} must be a non-empty string")
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
    )
