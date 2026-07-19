"""Spike S12: can a chat object be RENAMED through the local API?

WP21 (Claude-app-style auto-titling) wants the bot to set a chat's title
after the first exchange. The chat surface we have spiked (S10, quirks
C1-C8) covers create/list chats, full message CRUD, and SSE -- but no
spike, client wrapper, or mock route has ever updated a chat OBJECT.
Chats live in their own resource namespace (`/chats`), so the generic
`PATCH /objects/:id` has never been pointed at one either. Claims:

  (A) PATCH /v1/spaces/:sid/chats/:cid {"name": ...} renames the chat
      (endpoint may not exist at all -- 404/405 are live outcomes);
  (B) PATCH /v1/spaces/:sid/objects/:cid {"name": ...} renames it via
      the generic object-update route;
  (C) a chat created with NO name gets some default (what does a
      UI-created chat look like to list_chats?);
  (D) whichever PATCH "succeeds" is only believed if a fresh
      GET /chats re-list reflects the new name.

Surgical + self-cleaning posture: runs only in the GC-E2E space (found
by exact name, same rule as tests/e2e); creates throwaway chats; the
E2E reset sweeps spike artifacts anyway (and we attempt DELETE).

    ANYTYPE_API_KEY_FILE=.devcontainer/secrets/anytype_api_key \
    ANYTYPE_API_BASE_URL=http://anytype:31012 \
    python scripts/spike_chat_rename.py
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

    def list_name(chat_id: str) -> str | None:
        listing = request("GET", f"/v1/spaces/{space}/chats",
                          params={"limit": 100}).json()
        for c in listing.get("data", []):
            if c.get("id") == chat_id:
                return c.get("name")
        return "<NOT LISTED>"

    # Claim C first: a nameless chat.
    r = request("POST", f"/v1/spaces/{space}/chats", json={})
    print(f"create nameless chat: {r.status_code}")
    if r.status_code < 300:
        body = r.json().get("object") or r.json()
        print(f"  nameless chat envelope name={body.get('name')!r} "
              f"id={body.get('id')}")
        nameless_id = body.get("id")
        print(f"  re-list name: {list_name(nameless_id)!r}")
    else:
        print(f"  body: {r.text[:300]}")
        nameless_id = None

    # The rename target.
    r = request("POST", f"/v1/spaces/{space}/chats",
                json={"name": f"spike rename src {stamp}"})
    print(f"create named chat: {r.status_code}")
    if r.status_code >= 300:
        sys.exit(r.text[:500])
    chat = r.json().get("object") or r.json()
    chat_id = chat["id"]
    print(f"  chat_id: {chat_id}  layout={chat.get('layout')!r} "
          f"type_key={(chat.get('type') or {}).get('key')!r}")

    # Bonus: does a single-chat GET route even exist?
    r = request("GET", f"/v1/spaces/{space}/chats/{chat_id}")
    print(f"GET /chats/:cid: {r.status_code}")

    # Claim A: PATCH the chat route.
    r = request("PATCH", f"/v1/spaces/{space}/chats/{chat_id}",
                json={"name": f"renamed-a {stamp}"})
    print(f"(A) PATCH /chats/:cid: {r.status_code} "
          f"{'' if r.status_code < 300 else r.text[:300]}")
    print(f"    re-list name: {list_name(chat_id)!r}")

    # Claim B: PATCH the generic objects route.
    r = request("PATCH", f"/v1/spaces/{space}/objects/{chat_id}",
                json={"name": f"renamed-b {stamp}"})
    print(f"(B) PATCH /objects/:cid: {r.status_code} "
          f"{'' if r.status_code < 300 else r.text[:300]}")
    print(f"    re-list name: {list_name(chat_id)!r}")
    r = request("GET", f"/v1/spaces/{space}/objects/{chat_id}")
    if r.status_code < 300:
        obj = r.json().get("object") or r.json()
        print(f"    GET /objects/:cid name: {obj.get('name')!r}")
    else:
        print(f"    GET /objects/:cid: {r.status_code}")

    # Cleanup attempts (best effort; E2E reset sweeps anyway).
    for cid in filter(None, (chat_id, nameless_id)):
        r = request("DELETE", f"/v1/spaces/{space}/chats/{cid}")
        if r.status_code >= 300:
            r = request("DELETE", f"/v1/spaces/{space}/objects/{cid}")
        print(f"cleanup {cid}: {r.status_code}")


if __name__ == "__main__":
    main()
