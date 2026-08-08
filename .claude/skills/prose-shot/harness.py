"""Playwright visual-check harness for prose.html.

Loads the REAL page + vendored CodeMirror bundle with every network
request mocked via page.route — no inspection server, no Anytype, no
historian. Renders one tracked doc with every review state (plain /
needs_change / approved / locked / minor_revisions), drags a keyboard
selection from plain text into the needs_change paragraph, and
screenshots the editor in light and dark color schemes.

Run (chromium is preinstalled in the devcontainer image; the egress
firewall blocks fresh downloads, so the env var is load-bearing):

    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright python harness.py --out DIR

For a custom scenario (comments, view modes, different marks, save/409
flows) copy this file to the scratchpad and edit PARAS / doc_payload /
the gesture in shoot() — the wire shapes below are the contract.

Wire shapes (mirrors orchestrator/prose_bridge.py):
  GET api/prose/spaces -> {writes_enabled, spaces: [{space_id, label,
      error, nodes: [{id, name, revisions, last_at, last_author}]}]}
  GET api/prose/doc    -> {id, name, version, base, body, segments,
      spans, comments, revisions}
    segments: [{hash, start, end, status, intent, blame|null}]
    spans:    positional [start, end, author, status, intent]
      author: model|human   status: raw_ai|approved|human
      intent: flexible|needs_change|minor_revisions|locked
    offsets are CODE POINTS over body — ASCII bodies keep them equal
    to both Python and UTF-16 indices, so stick to ASCII fixtures.
  GET api/prose/events -> abort() is fine; EventSource retries quietly.
"""
import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[3]
PAGE = REPO / "src/graph_context/orchestrator/prose.html"
BUNDLE = REPO / "src/graph_context/orchestrator/static/codemirror.bundle.js"
NAV = REPO / "src/graph_context/orchestrator/static/nav.js"

PARAS = [
    "The captain stood at the rail and watched the horizon darken.",
    "A storm was coming in from the west, and every hand on deck "
    "knew it before the glass began to fall.",
    "She gave the order to reef the sails, her voice steady over "
    "the rising wind.",
    "Below decks, the cook lashed his pots to the beams and said "
    "nothing at all.",
    "The rain arrived all at once, the way it always does out here, "
    "and the deck went slick under it.",
]
# (author, status, intent) per paragraph: plain, amber, green, red, cyan
STATES = [
    ("model", "raw_ai", "flexible"),
    ("model", "raw_ai", "needs_change"),
    ("model", "approved", "flexible"),
    ("model", "approved", "locked"),
    ("model", "raw_ai", "minor_revisions"),
]
BODY = "\n\n".join(PARAS)


def offsets():
    out, cursor = [], 0
    for i, p in enumerate(PARAS):
        out.append((f"h{i}", cursor, cursor + len(p)))
        cursor += len(p) + 2
    return out


def doc_payload():
    offs = offsets()
    segments = [
        {"hash": h, "start": s, "end": e, "status": st, "intent": it,
         "blame": {"author": a, "detail": "test",
                   "at": "2026-01-01T12:00:00", "seq": 1}}
        for (h, s, e), (a, st, it) in zip(offs, STATES)
    ]
    spans = [[s, e, a, st, it]
             for (h, s, e), (a, st, it) in zip(offs, STATES)]
    return {
        "id": "n1", "name": "Chapter 1", "version": 1, "base": "b1",
        "body": BODY, "segments": segments, "spans": spans,
        "comments": [], "revisions": [
            {"seq": 1, "at": "2026-01-01T12:00:00", "author": "model",
             "detail": "test", "added": len(PARAS), "removed": 0}],
    }


SPACES = {
    "writes_enabled": True,
    "spaces": [{"space_id": "s1", "label": "Test space", "error": None,
                "nodes": [{"id": "n1", "name": "Chapter 1",
                           "revisions": 1, "last_at": None,
                           "last_author": None}]}],
}


def route_all(page):
    page.route("**/prose.html*", lambda r: r.fulfill(
        body=PAGE.read_text(), content_type="text/html"))
    page.route("**/static/codemirror.bundle.js", lambda r: r.fulfill(
        body=BUNDLE.read_text(), content_type="application/javascript"))
    page.route("**/static/nav.js", lambda r: r.fulfill(
        body=NAV.read_text(), content_type="application/javascript"))
    page.route("**/api/prose/spaces", lambda r: r.fulfill(
        body=json.dumps(SPACES), content_type="application/json"))
    page.route("**/api/prose/doc*", lambda r: r.fulfill(
        body=json.dumps(doc_payload()), content_type="application/json"))
    page.route("**/api/prose/events*", lambda r: r.abort())


def shoot(browser, scheme, out_dir):
    ctx = browser.new_context(
        viewport={"width": 860, "height": 900}, color_scheme=scheme)
    page = ctx.new_page()
    route_all(page)
    page.goto("http://prose.test/prose.html#/node/s1/n1")
    page.wait_for_selector(".cm-content")
    page.wait_for_timeout(400)
    # keyboard gesture: mid-paragraph 1 (plain) through paragraph 2
    # (needs_change) — mouse coordinates would be layout-fragile
    page.click(".cm-content")
    page.keyboard.press("Control+Home")
    for _ in range(4):
        page.keyboard.press("ArrowRight")
    for _ in range(3):
        page.keyboard.press("Shift+ArrowDown")
    page.keyboard.press("Shift+End")
    page.wait_for_timeout(300)
    path = out_dir / f"selection_{scheme}.png"
    page.locator("#editor-host").screenshot(path=str(path))
    check_input_attrs(page)
    ctx.close()
    return path


def check_input_attrs(page):
    """Pin the browser-input attributes on the live content DOM.

    CodeMirror hardcodes spellcheck="false" and only merges the page's
    contentAttributes facet over it, so this survives a bundle UPGRADE
    changing that merge -- which the source-level pytest pin cannot see.
    Headless Chromium ships no dictionary, so the squiggles never render
    in the screenshots; the attribute is the check.
    """
    want = {"spellcheck": "true", "autocorrect": "off",
            "autocapitalize": "off"}
    got = {name: page.get_attribute(".cm-content", name) for name in want}
    if got != want:
        raise SystemExit(f"content-DOM input attributes drifted: {got}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("."))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for scheme in ("light", "dark"):
            print(shoot(browser, scheme, args.out))
        browser.close()


if __name__ == "__main__":
    main()
