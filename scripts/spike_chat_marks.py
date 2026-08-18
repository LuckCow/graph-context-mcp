"""Spike S14: chat message MARKS (rich-text formatting) on the local API.

The chat UI renders message text as plain text (quirk C7), so the bot's
markdown links show their literal glyphs. The documented cure is the
``marks`` array on message create -- ``{"from", "to", "type", "param"}``
ranges over ``text`` (types: bold, italic, underscored, strikethrough,
keyboard, link, object, mention, emoji, text_color, background_color).
Claims to pin before the client grows a converter:

  (A) POST .../messages with ``marks`` is accepted, and a GET round-trip
      shows where marks live on the message (``content.marks``?).
  (B) PATCH replaces marks WHOLESALE like text/attachments (C8): an edit
      without ``marks`` drops them; an edit with new marks lands them.
  (C) Offset unit probe: does the server bounds-check ``to`` against the
      text length? Text "🙂 link" is 6 code points but 7 UTF-16 units --
      if bounds are validated, to=7 vs to=8 outcomes name the unit.
  (D) Junk tolerance: unknown ``type``, negative/inverted ranges, a link
      without ``param`` -- 400s or silent acceptance?
  (E) SSE: does the ``message_added`` frame carry the marks too?

Surgical + self-cleaning posture: runs only in the GC-E2E space (found
by exact name, same rule as tests/e2e); creates one throwaway chat; the
E2E reset sweeps spike artifacts anyway (and we attempt DELETE).

    ANYTYPE_API_KEY_FILE=.devcontainer/secrets/anytype_api_key \
    ANYTYPE_API_BASE_URL=http://anytype:31012 \
    python scripts/spike_chat_marks.py
"""

from __future__ import annotations

import json
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

    r = request("POST", f"/v1/spaces/{space}/chats",
                json={"name": "spike marks"})
    if r.status_code >= 300:
        sys.exit(r.text[:500])
    chat_id = (r.json().get("object") or r.json())["id"]
    print(f"chat: {chat_id}")
    messages_path = f"/v1/spaces/{space}/chats/{chat_id}/messages"

    def post(label: str, body: dict) -> str | None:
        r = request("POST", messages_path, json=body)
        print(f"{label}: {r.status_code} "
              f"{'' if r.status_code < 300 else r.text[:300]}")
        return r.json().get("message_id") if r.status_code < 300 else None

    def stored(message_id: str) -> dict | None:
        window = request("GET", messages_path,
                         params={"limit": 100}).json()["messages"]
        return next((m for m in window if m["id"] == message_id), None)

    # (A) the documented happy path, round-tripped.
    text = "See the API docs for details"
    happy = post("(A) link mark", {
        "text": text,
        "marks": [{"from": 8, "to": 16, "type": "link",
                   "param": "https://developers.anytype.io"}],
    })
    if happy:
        print(f"    stored: {json.dumps(stored(happy), default=str)[:600]}")

    # (B) C8 wholesale-replacement check for marks.
    if happy:
        r = request("PATCH", f"{messages_path}/{happy}",
                    json={"text": "edited, no marks"})
        print(f"(B) edit w/o marks: {r.status_code}")
        print(f"    stored: {json.dumps(stored(happy), default=str)[:400]}")
        r = request("PATCH", f"{messages_path}/{happy}", json={
            "text": "edited bold",
            "marks": [{"from": 7, "to": 11, "type": "bold"}],
        })
        print(f"    edit with bold: {r.status_code}")
        print(f"    stored: {json.dumps(stored(happy), default=str)[:400]}")

    # (C) offset unit probe: "🙂 link" = 6 code points, 7 UTF-16 units.
    emoji_text = "\N{SLIGHTLY SMILING FACE} link"
    for to in (6, 7, 8, 99):
        mid = post(f"(C) emoji text, mark to={to}", {
            "text": emoji_text,
            "marks": [{"from": 2, "to": to, "type": "link",
                       "param": "https://example.com"}],
        })
        if mid:
            kept = (stored(mid) or {})
            print(f"    stored marks: "
                  f"{json.dumps(kept.get('content', {}), default=str)[:300]}")

    # (D) junk tolerance.
    post("(D) unknown type", {
        "text": "junk type",
        "marks": [{"from": 0, "to": 4, "type": "sparkles"}],
    })
    post("(D) inverted range", {
        "text": "inverted",
        "marks": [{"from": 5, "to": 2, "type": "bold"}],
    })
    post("(D) negative from", {
        "text": "negative",
        "marks": [{"from": -1, "to": 3, "type": "bold"}],
    })
    post("(D) link without param", {
        "text": "no param",
        "marks": [{"from": 0, "to": 8, "type": "link"}],
    })
    post("(D) marks not a list", {"text": "bad marks", "marks": {"from": 0}})

    # cleanup (best effort; E2E reset sweeps anyway).
    r = request("DELETE", f"/v1/spaces/{space}/chats/{chat_id}")
    if r.status_code >= 300:
        r = request("DELETE", f"/v1/spaces/{space}/objects/{chat_id}")
    print(f"cleanup {chat_id}: {r.status_code}")


if __name__ == "__main__":
    main()
