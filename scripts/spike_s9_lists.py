"""Spike S9: probe the never-used lists/views endpoints against a LIVE server.

Answers (record in docs/WORK_PACKAGES.md under WP13, S1-S8 convention):
  S9a  does /lists/{list_id}/... accept a query-layout (Set) object id?
  S9b  do views expose machine-readable filter/sort definitions?
  S9c  does the view-objects endpoint apply filter AND sort server-side?
  S9d  page cap on view objects (100 like /search?) + properties inline?
  S9e  are Sets/Collections discoverable via /search or GET /objects?
  S9f  does a checkbox toggle show up on the next view-objects read?

Run (host desktop app must be up; devcontainer env already points at it):

    PYTHONPATH=src python scripts/spike_s9_lists.py

The script only touches the space named exactly GC-E2E (same safety rule
as tests/e2e). It seeds a `spike_todo` type + a handful of todos, then
looks for a Set named "S9 Spike Set". The local API has never been seen
to create Sets, so the first run will likely ask you to create it by hand
in the desktop app (a Query over spike_todo: filter Done unchecked, sort
Due date asc) and run the script again. Probes print raw JSON so quirks
land in WORK_PACKAGES verbatim; nothing here is production code.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any

import httpx

from graph_context.domain.models import NodeDraft
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.config import AnytypeConfig
from graph_context.infrastructure.anytype.repository import AnytypeGraphRepository

SPACE_NAME = "GC-E2E"
SET_NAME = "S9 Spike Set"
TODO_TYPE_KEY = "spike_todo"
API_VERSION = "2025-11-08"

TODOS = [
    # (name, done, due_date, priority)
    ("S9 pay taxes", True, "2026-07-01", "High"),
    ("S9 buy milk", False, "2026-07-10", "Low"),
    ("S9 write report", False, "2026-07-09", "High"),
    ("S9 call mom", False, None, "Medium"),
    ("S9 archive files", True, None, None),
]


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
        or "http://host.docker.internal:31009"
    ).rstrip("/")


def show(label: str, response: httpx.Response, clip: int = 2500) -> Any:
    print(f"\n--- {label}\n    {response.request.method} "
          f"{response.request.url.path}?{response.request.url.query.decode()}"
          f" -> {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        print(f"    (non-JSON body) {response.text[:clip]}")
        return None
    text = json.dumps(payload, indent=2)
    print(text[:clip] + ("\n    ...[clipped]" if len(text) > clip else ""))
    return payload


class Spike:
    def __init__(self) -> None:
        self.http = httpx.Client(
            base_url=_base(),
            headers={
                "Authorization": f"Bearer {_key()}",
                "Anytype-Version": API_VERSION,
            },
            timeout=30.0,
        )
        self.space_id = self._find_space()
        print(f"space {SPACE_NAME}: {self.space_id}")

    def _find_space(self) -> str:
        data = self.http.get("/v1/spaces", params={"limit": 200}).json()
        for space in data.get("data", []):
            if space.get("name") == SPACE_NAME:
                return str(space["id"])
        sys.exit(
            f"no space named exactly {SPACE_NAME!r}; create it in the desktop "
            "app (or run the live E2E suite once) and rerun"
        )

    # -- seeding ----------------------------------------------------------

    def ensure_todo_type(self) -> None:
        types = self.http.get(
            f"/v1/spaces/{self.space_id}/types", params={"limit": 200}
        ).json()
        keys = {t.get("key") for t in types.get("data", [])}
        if TODO_TYPE_KEY not in keys:
            r = self.http.post(
                f"/v1/spaces/{self.space_id}/types",
                json={"key": TODO_TYPE_KEY, "name": "Spike Todo",
                      "plural_name": "Spike Todos", "layout": "basic"},
            )
            print(f"create type {TODO_TYPE_KEY}: {r.status_code}")
            time.sleep(1)
        for key, name, fmt in (
            ("done", "Done", "checkbox"),
            ("due_date", "Due date", "date"),
            ("priority", "Priority", "select"),
        ):
            r = self.http.post(
                f"/v1/spaces/{self.space_id}/properties",
                json={"key": key, "name": name, "format": fmt},
            )
            print(f"create property {key} ({fmt}): {r.status_code}"
                  " (409/400 = already exists: fine)")
            time.sleep(1)

    def seed_todos(self) -> list[str]:
        """Seed through OUR adapter: it already speaks the write quirks
        (select values resolved to tags, fresh-property settle windows,
        checkbox booleans) so the spike probes lists, not writes."""

        async def _seed() -> list[str]:
            config = AnytypeConfig(
                api_key=_key(), space_id=self.space_id, base_url=_base()
            )
            client = AnytypeClient(config)
            repo = AnytypeGraphRepository(client)
            await repo.hydrate()
            existing = {n.name: n.id for n in repo.graph.nodes()}
            ids: list[str] = []
            for name, done, due, priority in TODOS:
                if name in existing:
                    ids.append(existing[name])
                    continue
                fields: dict[str, str] = {}
                if done:
                    fields["done"] = "true"
                if due:
                    fields["due_date"] = due
                if priority:
                    fields["priority"] = priority
                node = await repo.create_node(
                    NodeDraft(TODO_TYPE_KEY, name=name, summary=f"{name}.",
                              fields=fields)
                )
                ids.append(node.id)
                print(f"created {name!r}: {node.id} fields={node.fields}")
            await client.aclose()
            return ids

        return asyncio.run(_seed())

    # -- discovery (S9e) ---------------------------------------------------

    def find_set(self) -> str | None:
        # Probe 1: plain name search (does a Set object surface at all?).
        by_name = show(
            "S9e: POST /search for the Set by name",
            self.http.post(
                f"/v1/spaces/{self.space_id}/search",
                params={"limit": 50},
                json={"query": SET_NAME},
            ),
        )
        # Probe 2: layout-typed search (sets/collections often type ot-set /
        # ot-collection; unknown whether search types accepts them).
        for guess in ("set", "collection", "ot-set", "query"):
            r = self.http.post(
                f"/v1/spaces/{self.space_id}/search",
                params={"limit": 10},
                json={"query": "", "types": [guess]},
            )
            print(f"S9e: search types=[{guess!r}] -> {r.status_code}, "
                  f"{len(r.json().get('data', [])) if r.status_code == 200 else '-'} hits")
        if by_name:
            for obj in by_name.get("data", []):
                if obj.get("name") == SET_NAME:
                    print(f"found {SET_NAME!r}: id={obj['id']} "
                          f"layout={obj.get('layout')!r} type={obj.get('type')!r}")
                    return str(obj["id"])
        return None

    def try_create_set(self) -> str | None:
        """The API has never been seen to create Sets -- probe anyway."""
        for type_key in ("set", "ot-set", "collection"):
            r = self.http.post(
                f"/v1/spaces/{self.space_id}/objects",
                json={"type_key": type_key, "name": SET_NAME},
            )
            print(f"S9: create object type_key={type_key!r} -> {r.status_code}")
            if r.status_code < 300:
                return str(r.json()["object"]["id"])
            time.sleep(1)
        return None

    # -- the list/view probes (S9a-S9d) -------------------------------------

    def probe_list(self, list_id: str, todo_ids: list[str]) -> None:
        views = show(
            "S9a/S9b: GET list views (raw -- look for filters/sorts objects)",
            self.http.get(
                f"/v1/spaces/{self.space_id}/lists/{list_id}/views",
                params={"limit": 50},
            ),
        )
        view_ids = [str(v["id"]) for v in (views or {}).get("data", [])
                    if isinstance(v, dict) and "id" in v]
        print(f"view ids: {view_ids}")

        # Objects WITHOUT a view (both documented path shapes).
        for path in (
            f"/v1/spaces/{self.space_id}/lists/{list_id}/objects",
            f"/v1/spaces/{self.space_id}/lists/{list_id}/views/objects",
        ):
            show("S9a: view-less objects fetch", self.http.get(
                path, params={"limit": 10}))

        for view_id in view_ids[:2]:
            for path in (
                f"/v1/spaces/{self.space_id}/lists/{list_id}/{view_id}/objects",
                f"/v1/spaces/{self.space_id}/lists/{list_id}/views/{view_id}/objects",
            ):
                payload = show(
                    "S9c/S9d: view objects (check order, filtering, inline "
                    "properties)",
                    self.http.get(path, params={"limit": 200}),
                )
                if payload and payload.get("data"):
                    names = [o.get("name") for o in payload["data"]]
                    print(f"    names in order: {names}")
                    print(f"    S9d: requested limit=200, got "
                          f"{len(payload['data'])} "
                          f"(pagination block: {payload.get('pagination')})")
                    break

        # S9f: toggle a checkbox, re-read the first working view.
        if todo_ids and view_ids:
            target = todo_ids[0]
            r = self.http.patch(
                f"/v1/spaces/{self.space_id}/objects/{target}",
                json={"properties": [{"key": "done", "checkbox": True}]},
            )
            print(f"\nS9f: PATCH done=true on {target} -> {r.status_code}")
            time.sleep(2)
            show("S9f: re-read view objects after toggle", self.http.get(
                f"/v1/spaces/{self.space_id}/lists/{list_id}/{view_ids[0]}/objects",
                params={"limit": 200},
            ))
            # put it back so reruns start clean
            self.http.patch(
                f"/v1/spaces/{self.space_id}/objects/{target}",
                json={"properties": [{"key": "done", "checkbox": False}]},
            )


def main() -> None:
    spike = Spike()
    spike.ensure_todo_type()
    todo_ids = spike.seed_todos()
    print(f"\nseeded todo ids: {todo_ids}")

    list_id = spike.find_set() or spike.try_create_set()
    if list_id is None:
        sys.exit(
            f"\nNo Set found. In the desktop app, inside {SPACE_NAME!r}:\n"
            f"  1. create a Query (Set) named exactly {SET_NAME!r}\n"
            f"     over the type 'Spike Todo'\n"
            "  2. add filter: Done is unchecked\n"
            "  3. add sorts: Due date ascending, then Priority\n"
            "then rerun this script to run the S9a-S9f probes."
        )
    print(
        f"\nNOTE: if {SET_NAME!r} was API-created it has NO source type "
        "(the API exposes none), so view-objects may 500. In the desktop "
        "app open it, set its query source to 'Spike Todo', add filter "
        "Done unchecked + sorts (Due date asc, Priority), then rerun."
    )
    spike.probe_list(list_id, todo_ids)
    print(
        "\nDone. Record S9a-S9f answers in docs/WORK_PACKAGES.md (WP13); "
        "quirks go to mapping.py + mock_server.py when built upon."
    )


if __name__ == "__main__":
    main()
