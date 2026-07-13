"""Attribution field keys: generation provenance as REAL properties (ADR 028).

The recorders (intent, capture) stamp who/what/when onto the nodes they
write. Since ADR 028 these land in native Anytype properties -- visible,
filterable, editable in the UI -- never in a JSON side-channel (the old
``gc_fields`` blob is retired). The keys live here, like the Scheduled
Event keys in :mod:`graph_context.domain.scheduling`, so the application
recorders and the storage adapters share one vocabulary; the Anytype
adapter mints them at bootstrap with human display names.
"""

from __future__ import annotations

FIELD_GENERATED_AT = "gc_generated_at"  # ISO timestamp of the generating turn
FIELD_USER_ID = "gc_user_id"  # who asked (transport-scoped id)
FIELD_MODEL = "gc_model"  # which model produced it
FIELD_MODE = "gc_mode"  # the activity mode that allowed the mutation
FIELD_ORIGIN = "gc_origin"  # transport pointer to the triggering message

# key -> property format; every attribution value is a plain string.
ATTRIBUTION_FIELDS: dict[str, str] = {
    FIELD_GENERATED_AT: "text",
    FIELD_USER_ID: "text",
    FIELD_MODEL: "text",
    FIELD_MODE: "text",
    FIELD_ORIGIN: "text",
}
