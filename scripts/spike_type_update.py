"""Spike: can PATCH /v1/spaces/:space/types/:id ADD properties to a type?

WP19 fallout: `gc_mode_activity_detail` was added to the Activity Mode
field set, but ensure_schema only creates MISSING types -- a space whose
`gc_activity_mode` type predates the field never shows it in the editor.
Retrofitting needs the type-update endpoint, whose semantics are
unverified. Three claims to settle against the live server:

  (A) PATCH with the full current property list + one new entry ADDS the
      new field and keeps the rest (no wholesale loss);
  (B) PATCH whose `properties` omits existing entries STRIPS them
      (i.e. the list is wholesale, like chat-message edits, C8) -- or
      merges; whichever it is, the retrofit must send the full list;
  (C) an entry naming an ALREADY-MINTED space property key attaches that
      property (the bootstrap comment marks this unverified for inline
      CREATE; the retrofit needs it for update).

Surgical + self-cleaning posture: runs only in the GC-E2E space (found by
exact name, same rule as tests/e2e), creates one throwaway type and one
throwaway property; the E2E reset sweeps spike artifacts anyway.

    ANYTYPE_API_KEY_FILE=/run/secrets/anytype_api_key \
    ANYTYPE_API_BASE_URL=http://anytype:31012 \
    python scripts/spike_type_update.py
"""

from __future__ import annotations

import os
import sys
import time

import httpx

API_VERSION = "2025-11-08"
SPACE_NAME = "GC-E2E"


def _key() -> str:
    if os.environ.get("ANYTYPE_API_KEY"):
        return os.environ["ANYTYPE_API_KEY"]
    path = os.environ.get("ANYTYPE_API_KEY_FILE", "")
    if path and os.path.exists(path):
        return open(path).read().strip()
    sys.exit("no ANYTYPE_API_KEY / ANYTYPE_API_KEY_FILE")


def _base() -> str:
    return (
        os.environ.get("ANYTYPE_BASE_URL")
        or os.environ.get("ANYTYPE_API_BASE_URL")
        or "http://localhost:31009"
    )


def main() -> None:
    base = _base()
    headers = {
        "Authorization": f"Bearer {_key()}",
        "Anytype-Version": API_VERSION,
    }

    def request(method: str, path: str, **kw):
        for attempt in range(8):
            r = httpx.request(method, f"{base}{path}", headers=headers,
                              timeout=30, **kw)
            if r.status_code not in (429, 500, 502, 503, 504):
                return r
            time.sleep(0.5 * (attempt + 1))
        return r

    spaces = request("GET", "/v1/spaces", params={"limit": 100}).json()["data"]
    space = next((s["id"] for s in spaces if s.get("name") == SPACE_NAME), None)
    if space is None:
        sys.exit(f"no space named {SPACE_NAME!r}; run the E2E suite once")
    print(f"space: {space}")

    stamp = str(int(time.time()))[-6:]
    type_key = f"gc_spike_tu_{stamp}"
    prop_a, prop_b, prop_c = (f"gc_spike_tu_{x}_{stamp}" for x in "abc")

    # A standalone space property, minted BEFORE the type exists (claim C).
    r = request("POST", f"/v1/spaces/{space}/properties",
                json={"key": prop_c, "name": "Spike C", "format": "text"})
    print(f"mint standalone property {prop_c}: {r.status_code}")

    # The throwaway type, born with one inline property.
    r = request("POST", f"/v1/spaces/{space}/types", json={
        "key": type_key, "name": f"Spike TU {stamp}",
        "plural_name": f"Spike TUs {stamp}", "layout": "basic",
        "properties": [{"key": prop_a, "name": "Spike A", "format": "text"}],
    })
    print(f"create type {type_key}: {r.status_code}")
    if r.status_code >= 300:
        sys.exit(r.text[:500])
    created = r.json().get("type") or r.json()
    type_id = created["id"]

    def type_props() -> list[str]:
        obj = request("GET", f"/v1/spaces/{space}/types/{type_id}").json()
        body = obj.get("type") or obj
        return [p.get("key") for p in body.get("properties", [])]

    print(f"props after create: {type_props()}")

    # Claim A + C: full list + one NEW property + the pre-minted one.
    full = [
        {"key": prop_a, "name": "Spike A", "format": "text"},
        {"key": prop_b, "name": "Spike B", "format": "text"},   # new
        {"key": prop_c, "name": "Spike C", "format": "text"},   # pre-minted
    ]
    r = request("PATCH", f"/v1/spaces/{space}/types/{type_id}", json={
        "name": f"Spike TU {stamp}", "plural_name": f"Spike TUs {stamp}",
        "layout": "basic", "properties": full,
    })
    print(f"PATCH add (full list): {r.status_code} "
          f"{'' if r.status_code < 300 else r.text[:300]}")
    print(f"props after add: {type_props()}")

    # Claim B: a properties list with ONLY prop_b -- wholesale or merge?
    r = request("PATCH", f"/v1/spaces/{space}/types/{type_id}", json={
        "name": f"Spike TU {stamp}", "plural_name": f"Spike TUs {stamp}",
        "layout": "basic",
        "properties": [{"key": prop_b, "name": "Spike B", "format": "text"}],
    })
    print(f"PATCH shrink (only B): {r.status_code}")
    print(f"props after shrink: {type_props()}")

    # Bonus: PATCH WITHOUT a properties field at all -- untouched or cleared?
    r = request("PATCH", f"/v1/spaces/{space}/types/{type_id}", json={
        "name": f"Spike TU renamed {stamp}",
        "plural_name": f"Spike TUs {stamp}", "layout": "basic",
    })
    print(f"PATCH no-properties: {r.status_code}")
    print(f"props after no-properties: {type_props()}")

    # Cleanup: archive the type if the API allows.
    r = request("DELETE", f"/v1/spaces/{space}/types/{type_id}")
    print(f"DELETE type: {r.status_code}")


if __name__ == "__main__":
    main()
