"""Phase-0 spike: does a template's DEFAULT PROPERTY VALUE apply on create, and
does an explicitly-supplied property OVERRIDE it?

The body-collision spike (docs/spikes/templates-body-collision.md) proved a
template's *body* applies on `template_id`-create. It did NOT test properties.
The whole template-aware-create plan rests on two unproven claims:

  (A) creating with `template_id` alone applies the template's default property
      values (e.g. status = "To Do");
  (B) passing an explicit value for that same property OVERRIDES the default
      rather than being ignored.

This probe settles both against a real property-defaulting template. It needs a
space that owns a template whose type has a `select` property defaulted on the
template -- the Todolist "Task" template (status -> "To Do") fits. Surgical +
self-cleaning: creates two throwaway objects, reads them back, archives both in
a finally block. Never resets a space.

    ANYTYPE_API_KEY_FILE=/run/secrets/anytype_api_key \
    ANYTYPE_API_BASE_URL=http://anytype:31012 \
    python scripts/spike_template_props.py [SpaceName]

SpaceName defaults to "Todolist".
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx

API_VERSION = "2025-11-08"
DEFAULT_SPACE = "Todolist"


def _key() -> str:
    if os.environ.get("ANYTYPE_API_KEY"):
        return os.environ["ANYTYPE_API_KEY"]
    path = os.environ.get("ANYTYPE_API_KEY_FILE")
    if path and os.path.exists(path):
        with open(path) as handle:
            return handle.read().strip()
    sys.exit("no ANYTYPE_API_KEY / ANYTYPE_API_KEY_FILE in the environment")


def _base() -> str:
    return (
        os.environ.get("ANYTYPE_BASE_URL")
        or os.environ.get("ANYTYPE_API_BASE_URL")
        or "http://anytype:31012"
    ).rstrip("/")


class Probe:
    def __init__(self, space_name: str) -> None:
        self.http = httpx.Client(
            base_url=_base(),
            headers={
                "Authorization": f"Bearer {_key()}",
                "Anytype-Version": API_VERSION,
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        self.space_id = self._find_space(space_name)
        self.created: list[str] = []

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        r = self.http.get(path, params=params or None)
        r.raise_for_status()
        return r.json()

    def _find_space(self, name: str) -> str:
        for s in self._get("/v1/spaces", limit=200).get("data", []):
            if s.get("name") == name:
                return str(s["id"])
        sys.exit(f"no space named {name!r}")

    def _sp(self, path: str) -> str:
        return f"/v1/spaces/{self.space_id}{path}"

    # -- discovery -------------------------------------------------------------

    def find_task_template(self) -> tuple[str, str, dict[str, Any]]:
        """Return (type_key, template_id, status_property) for the first type
        that has BOTH a template and a `select` property. status_property carries
        {id, key, tags:[{key,name}...]}."""
        for typ in self._get(self._sp("/types"), limit=100).get("data", []):
            type_id, type_key = typ["id"], typ["key"]
            tpls = self._get(self._sp(f"/types/{type_id}/templates"), limit=10).get("data", [])
            if not tpls:
                continue
            select_prop = next(
                (p for p in typ.get("properties", []) if p.get("format") == "select"), None
            )
            if select_prop is None:
                continue
            prop_id = select_prop["id"]
            tag_data = self._get(self._sp(f"/properties/{prop_id}/tags"), limit=50)
            tags = [
                {"key": t["key"], "name": t.get("name", "")}
                for t in tag_data.get("data", [])
            ]
            return type_key, tpls[0]["id"], {
                "id": prop_id, "key": select_prop["key"], "tags": tags,
            }
        sys.exit("no type in this space has both a template and a select property")

    # -- create / read ---------------------------------------------------------

    def create(self, body: dict[str, Any], label: str) -> str | None:
        r = self.http.post(self._sp("/objects"), content=json.dumps(body))
        if r.status_code >= 400:
            print(f"  [{label}] HTTP {r.status_code}: {r.text[:200]}")
            return None
        oid = r.json().get("object", {}).get("id")
        if oid:
            self.created.append(oid)
        return oid

    def read_select(self, object_id: str, prop_key: str) -> str | None:
        obj = self._get(self._sp(f"/objects/{object_id}")).get("object", {})
        for p in obj.get("properties", []):
            if p.get("key") == prop_key:
                val = p.get("select")
                if isinstance(val, dict):
                    return val.get("name") or val.get("key")
                return val
        return None

    def cleanup(self) -> None:
        for oid in self.created:
            try:
                self.http.delete(self._sp(f"/objects/{oid}"))
            except httpx.HTTPError as exc:
                print(f"  cleanup failed for {oid}: {exc}")
        print(f"cleaned up {len(self.created)} object(s)")


def main() -> None:
    space_name = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SPACE
    probe = Probe(space_name)
    try:
        type_key, template_id, status = probe.find_task_template()
        tags = status["tags"]
        print(f"space={space_name!r} type={type_key!r} template={template_id[:12]}…")
        print(f"select property {status['key']!r} tags: {[t['name'] for t in tags]}\n")

        # (A) template only -> what default lands?
        oid_a = probe.create(
            {"name": "spike-tplprop-A", "type_key": type_key, "template_id": template_id},
            "template-only",
        )
        default_val = probe.read_select(oid_a, status["key"]) if oid_a else None
        print(f"(A) template only        -> {status['key']} = {default_val!r}")

        # Pick an alternative tag different from the observed default.
        alt = next(
            (t for t in tags if t["name"] != default_val and t["key"] != default_val), None
        )
        if alt is None:
            print("no alternative tag to test override with; property has <2 options")
            override_val = None
        else:
            # (B) template + explicit different value -> override?
            oid_b = probe.create(
                {
                    "name": "spike-tplprop-B",
                    "type_key": type_key,
                    "template_id": template_id,
                    "properties": [
                        {"key": status["key"], "format": "select", "select": alt["key"]}
                    ],
                },
                "template+explicit",
            )
            override_val = probe.read_select(oid_b, status["key"]) if oid_b else None
            print(f"(B) template + {alt['name']!r:12} -> {status['key']} = {override_val!r}")

        print("\n=== VERDICT ===")
        default_applies = default_val is not None
        print(f"(A) default property applies on create: {default_applies} "
              f"(got {default_val!r})")
        if alt is not None:
            overrides = override_val == alt["name"] or override_val == alt["key"]
            print(f"(B) explicit value overrides default : {overrides} "
                  f"(sent {alt['name']!r}, got {override_val!r})")
            if default_applies and overrides:
                print("\n>>> GREEN: build the plan as designed (single POST).")
            else:
                print("\n>>> RED: reassess — explicit value did not override; a "
                      "follow-up PATCH is needed for overridden properties.")
    finally:
        probe.cleanup()
        probe.http.close()


if __name__ == "__main__":
    main()
