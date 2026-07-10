"""Spike: what does the Anytype API do with create + a type template + a body?

Settles the "unspiked collision risk" flagged in docs/WORK_PACKAGES.md (the
"Templates: skipped deliberately" decision): when you create an object and hand
the API BOTH a template (which carries its own body) AND an explicit ``body``,
which body survives? Also probes whether a template can be minted via the API at
all, and which request field name actually triggers template application.

Surgical + self-cleaning: it only ever *creates* objects, tracks their ids, and
archives them in a ``finally`` block. It never resets or mass-deletes a space, so
it is safe to point at a working space that already owns a template. It does NOT
depend on the production client -- raw httpx, like scripts/spike_s10_chat.py.

    ANYTYPE_API_KEY_FILE=/run/secrets/anytype_api_key \
    ANYTYPE_API_BASE_URL=http://anytype:31012 \
    python scripts/spike_templates.py

Findings are printed and also written to docs/spikes/templates-body-collision.md.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx

API_VERSION = "2025-11-08"

# A distinctive body we supply explicitly; if it shows up in the created object's
# markdown we know the caller-supplied body won.
CUSTOM_MARKER = "SPIKE-CUSTOM-BODY the-quick-brown-fox-9f3a"

# Candidate request field names for "apply this template on create". The Anytype
# REST API documents ``template_id``; we try the alternates too so the spike
# reports the truth rather than assuming.
TEMPLATE_FIELDS = ["template_id", "templateId", "template"]

OUT_DOC = "docs/spikes/templates-body-collision.md"


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


class Spike:
    def __init__(self) -> None:
        self.http = httpx.Client(
            base_url=_base(),
            headers={
                "Authorization": f"Bearer {_key()}",
                "Anytype-Version": API_VERSION,
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        self.created: list[tuple[str, str]] = []  # (space_id, object_id) for cleanup

    # -- helpers ---------------------------------------------------------------

    def _get(self, path: str) -> dict[str, Any]:
        r = self.http.get(path)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict[str, Any]) -> httpx.Response:
        return self.http.post(path, content=json.dumps(body))

    def _paged(self, path: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self._get(f"{path}?limit=100&offset={offset}")
            data = page.get("data", [])
            out.extend(data)
            if len(data) < 100:
                return out
            offset += 100

    def _markdown_of(self, space_id: str, object_id: str) -> str | None:
        obj = self._get(f"/v1/spaces/{space_id}/objects/{object_id}").get("object", {})
        return obj.get("markdown")

    def create(
        self, space_id: str, body: dict[str, Any], label: str
    ) -> str | None:
        """POST an object; track it for cleanup; return its id (or None on error)."""
        r = self._post(f"/v1/spaces/{space_id}/objects", body)
        if r.status_code >= 400:
            print(f"  [{label}] HTTP {r.status_code}: {r.text[:200]}")
            return None
        obj = r.json().get("object", {})
        oid = obj.get("id")
        if oid:
            self.created.append((space_id, oid))
        return oid

    # -- discovery -------------------------------------------------------------

    def find_template(self) -> dict[str, Any] | None:
        """Scan every space for the first (type, template) pair that exists.

        Prefers a space named GC-E2E if it happens to own a template; otherwise
        takes the first template found anywhere and reports loudly which space.
        """
        spaces = self._paged("/v1/spaces")
        # Stable order, GC-E2E first if present.
        spaces.sort(key=lambda s: (s.get("name") != "GC-E2E", s.get("name", "")))
        for space in spaces:
            sid = space["id"]
            for typ in self._paged(f"/v1/spaces/{sid}/types"):
                tid = typ["id"]
                try:
                    tpls = self._get(
                        f"/v1/spaces/{sid}/types/{tid}/templates?limit=5"
                    ).get("data", [])
                except httpx.HTTPStatusError:
                    continue
                if tpls:
                    tpl = tpls[0]
                    md = self._markdown_of(sid, tpl["id"])
                    return {
                        "space_id": sid,
                        "space_name": space.get("name"),
                        "type_key": typ["key"],
                        "type_name": typ.get("name"),
                        "type_id": tid,
                        "template_id": tpl["id"],
                        "template_name": tpl.get("name"),
                        "template_markdown": md,
                    }
        return None

    def probe_template_creation(self, space_id: str) -> dict[str, Any]:
        """Can a template be minted through the API? (type_key=template)."""
        r = self._post(
            f"/v1/spaces/{space_id}/objects",
            {
                "name": "spike-probe-template",
                "type_key": "template",
                "body": "PROBE_TEMPLATE_BODY",
            },
        )
        if r.status_code < 400:
            obj = r.json().get("object", {})
            oid = obj.get("id")
            if oid:
                self.created.append((space_id, oid))
            return {"status": r.status_code, "created": True, "id": oid}
        return {"status": r.status_code, "created": False, "body": r.text[:200]}

    # -- the matrix ------------------------------------------------------------

    def run_matrix(self, ctx: dict[str, Any]) -> dict[str, Any]:
        sid = ctx["space_id"]
        type_key = ctx["type_key"]
        tpl_id = ctx["template_id"]
        tpl_md = ctx["template_markdown"] or ""
        # A short distinctive slice of the template body to test for its presence.
        tpl_needle = _needle(tpl_md)

        results: dict[str, Any] = {}

        # First: which request field name actually applies the template? Create
        # template-only objects under each candidate field and see which one
        # yields the template's body.
        field_used: str | None = None
        for field in TEMPLATE_FIELDS:
            oid = self.create(
                sid,
                {"name": f"spike-tplonly-{field}", "type_key": type_key, field: tpl_id},
                f"template-only via {field}",
            )
            md = self._markdown_of(sid, oid) if oid else None
            applied = bool(tpl_needle) and tpl_needle in (md or "")
            results.setdefault("template_field_probe", {})[field] = {
                "object_id": oid,
                "markdown": md,
                "template_applied": applied,
            }
            if applied and field_used is None:
                field_used = field

        results["template_field_that_works"] = field_used

        # Case B: body only, no template.
        oid_b = self.create(
            sid,
            {"name": "spike-bodyonly", "type_key": type_key, "body": CUSTOM_MARKER},
            "body-only",
        )
        md_b = self._markdown_of(sid, oid_b) if oid_b else None
        results["body_only"] = {
            "object_id": oid_b,
            "markdown": md_b,
            "custom_applied": CUSTOM_MARKER in (md_b or ""),
        }

        # Case C: the crux -- template AND body together.
        field = field_used or "template_id"
        oid_c = self.create(
            sid,
            {
                "name": "spike-template-plus-body",
                "type_key": type_key,
                field: tpl_id,
                "body": CUSTOM_MARKER,
            },
            f"template+body via {field}",
        )
        md_c = self._markdown_of(sid, oid_c) if oid_c else None
        results["template_plus_body"] = {
            "field": field,
            "object_id": oid_c,
            "markdown": md_c,
            "template_applied": bool(tpl_needle) and tpl_needle in (md_c or ""),
            "custom_applied": CUSTOM_MARKER in (md_c or ""),
        }
        return results

    # -- cleanup ---------------------------------------------------------------

    def cleanup(self) -> None:
        for sid, oid in self.created:
            try:
                self.http.delete(f"/v1/spaces/{sid}/objects/{oid}")
            except httpx.HTTPError as exc:  # best-effort; report, don't crash
                print(f"  cleanup failed for {oid}: {exc}")
        print(f"cleaned up {len(self.created)} object(s)")


def _needle(markdown: str) -> str:
    """A distinctive, whitespace-trimmed slice of a template body for presence checks."""
    stripped = markdown.strip()
    return stripped[:40] if stripped else ""


def _order(ctx: dict[str, Any], results: dict[str, Any]) -> str | None:
    """When both bodies survive, report which comes first in the merged body."""
    c = results["template_plus_body"]
    md = c.get("markdown") or ""
    needle = _needle(ctx["template_markdown"] or "")
    ti = md.find(needle) if needle else -1
    ci = md.find(CUSTOM_MARKER)
    if ti < 0 or ci < 0:
        return None
    return "template-then-body" if ti < ci else "body-then-template"


def _verdict(ctx: dict[str, Any], results: dict[str, Any]) -> str:
    c = results["template_plus_body"]
    if c["object_id"] is None:
        return "template+body was REJECTED by the API (create failed)"
    if c["template_applied"] and c["custom_applied"]:
        order = _order(ctx, results)
        tail = {
            "template-then-body": " (template body first, supplied body appended after)",
            "body-then-template": " (supplied body first, template body appended after)",
        }.get(order or "", "")
        return "BOTH survive: the template body and the supplied body CONCATENATE" + tail
    if c["custom_applied"] and not c["template_applied"]:
        return "the supplied body WINS: it overrides the template body"
    if c["template_applied"] and not c["custom_applied"]:
        return "the template WINS: the supplied body is silently dropped"
    return "NEITHER body appears (object created empty)"


def _write_doc(ctx: dict[str, Any], tpl_probe: dict[str, Any], results: dict[str, Any]) -> None:
    verdict = _verdict(ctx, results)
    field = results.get("template_field_that_works")
    tpl_name = ctx.get("template_name") or "(unnamed default template)"
    lines = [
        "# Spike: create + template + body — which body wins?",
        "",
        "Settles the collision flagged in `docs/WORK_PACKAGES.md` under \"Templates:",
        "skipped deliberately\" — the interaction between a type template's body and",
        "our create-with-`body` path, previously unspiked.",
        "",
        f"Run against space **{ctx['space_name']}**, type **{ctx['type_name']}**",
        f"(`{ctx['type_key']}`), template **{tpl_name}**.",
        "Reproduce: `python scripts/spike_templates.py`.",
        "",
        "## Verdict",
        "",
        f"**{verdict}.**",
        "",
        "- Request field that applies a template on create: "
        + (f"`{field}` (the value is the template's object id)" if field
           else f"**none of {TEMPLATE_FIELDS} applied the template**"),
        f"- The same id under the other field names "
        f"({', '.join(f for f in TEMPLATE_FIELDS if f != field)}) was **ignored** "
        "— no template applied.",
        f"- Minting a template via the API (`type_key=template`): "
        f"HTTP {tpl_probe['status']} — "
        + ("succeeded" if tpl_probe.get("created") else "**not possible**"),
        "",
        "## What this means for our create-with-body path",
        "",
        "Passing a `template_id` does **not** replace or conflict with our `body` —"
        " the API stacks the template's body first and appends the supplied `body`"
        " after it. So a template can safely default *properties* (e.g. status = To"
        " Do) on create, but if the type's template also carries body scaffolding,"
        " any `body` we send lands **below** it rather than as the whole page. A"
        " template is not a way to override the body; it is additive.",
        "",
        "Note: the read-back `markdown` prepends the object's name as its first line"
        " (a note-layout quirk) — ignore that line when reading the tables below.",
        "",
        "## Bodies observed",
        "",
        "| create call | body read back |",
        "| --- | --- |",
        f"| template only (`{field or TEMPLATE_FIELDS[0]}`) | {_cell(_applied_md(results))} |",
        f"| body only | {_cell(results['body_only']['markdown'])} |",
        f"| template + body | {_cell(results['template_plus_body']['markdown'])} |",
        "",
        "Template's own body, for reference:",
        "",
        "```",
        (ctx["template_markdown"] or "(empty)"),
        "```",
        "",
        "## Raw results",
        "",
        "```json",
        json.dumps(
            {
                "context": {
                    k: ctx[k]
                    for k in ("space_name", "type_key", "template_name", "template_id")
                },
                "template_creation_probe": tpl_probe,
                "matrix": results,
            },
            indent=2,
        ),
        "```",
        "",
    ]
    os.makedirs(os.path.dirname(OUT_DOC), exist_ok=True)
    with open(OUT_DOC, "w") as handle:
        handle.write("\n".join(lines))
    print(f"wrote {OUT_DOC}")


def _applied_md(results: dict[str, Any]) -> str | None:
    field = results.get("template_field_that_works") or TEMPLATE_FIELDS[0]
    probe = results.get("template_field_probe", {}).get(field, {})
    return probe.get("markdown")


def _cell(md: str | None) -> str:
    if md is None:
        return "_(none)_"
    return "`" + md.replace("\n", "\\n").replace("|", "\\|")[:120] + "`"


def main() -> None:
    spike = Spike()
    try:
        ctx = spike.find_template()
        if ctx is None:
            sys.exit(
                "No space owns a template. Templates cannot be created via the API,"
                " so this spike needs a space with a UI-authored template."
            )
        print(f"using template {ctx['template_name']!r} on type {ctx['type_key']!r}"
              f" in space {ctx['space_name']!r}")
        print(f"template body: {ctx['template_markdown']!r}\n")

        tpl_probe = spike.probe_template_creation(ctx["space_id"])
        print(f"probe: create template via API -> HTTP {tpl_probe['status']} "
              f"(created={tpl_probe.get('created')})\n")

        results = spike.run_matrix(ctx)

        print("\n=== RESULTS ===")
        print(f"template field that works: {results['template_field_that_works']}")
        print(f"template-only body applied: "
              f"{_applied_md(results)!r}")
        print(f"body-only  -> custom_applied={results['body_only']['custom_applied']}")
        c = results["template_plus_body"]
        print(f"template+body ({c['field']}) -> template_applied={c['template_applied']}"
              f" custom_applied={c['custom_applied']}")
        print(f"\nVERDICT: {_verdict(ctx, results)}\n")

        _write_doc(ctx, tpl_probe, results)
    finally:
        spike.cleanup()
        spike.http.close()


if __name__ == "__main__":
    main()
