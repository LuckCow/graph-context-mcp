"""Spike S10: probe the Chat API (heart v0.50.7+) against a LIVE server.

Run now against the desktop endpoint; rerun after the WP14 sidecar
cutover for the deferred items (rate-limit disable, bot member id).

Answers (record in docs/WORK_PACKAGES.md WP14, S1-S9 convention):
  S10a  do the chat endpoints exist on this server's heart version?
  S10b  message payload schema (creator/text/order-id fields; markdown?)
  S10c  SSE frame shapes, backlog size on connect, heartbeat format
  S10d  how a client learns its own member id (members list / auth-me)
  S10e  can the API create chats? does a space have a default chat?
  S10f  message length cap

Run:  PYTHONPATH=src python scripts/spike_s10_chat.py

Uses the GC-E2E space (same safety rule as tests/e2e and spike S9:
refuses to touch any other space). Probes print raw JSON so quirks land
in WORK_PACKAGES verbatim; nothing here is production code.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import httpx

SPACE_NAME = "GC-E2E"
API_VERSION = "2025-11-08"
CHAT_NAME = "S10 Spike Chat"


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
          f"{response.request.url.raw_path.decode()} -> {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        print(f"    (non-JSON body) {response.text[:clip]}")
        return None
    text = json.dumps(payload, indent=2)
    print(text[:clip] + ("\n    ...[clipped]" if len(text) > clip else ""))
    return payload


async def main() -> None:
    headers = {
        "Authorization": f"Bearer {_key()}",
        "Anytype-Version": API_VERSION,
    }
    async with httpx.AsyncClient(
        base_url=_base(), headers=headers, timeout=30.0
    ) as http:
        spaces = (await http.get("/v1/spaces", params={"limit": 200})).json()
        space_id = next(
            (s["id"] for s in spaces.get("data", []) if s.get("name") == SPACE_NAME),
            None,
        )
        if space_id is None:
            sys.exit(f"no space named exactly {SPACE_NAME!r}; create/sync it first")
        print(f"space {SPACE_NAME}: {space_id}")

        # -- S10a: do chat endpoints exist at all? --------------------------
        chats = show(
            "S10a/S10e: GET chats (404 here = heart too old for chat API)",
            await http.get(f"/v1/spaces/{space_id}/chats", params={"limit": 50}),
        )
        if chats is None:
            sys.exit(
                "S10a: chat endpoints ABSENT on this server -- chat work "
                "proceeds mock-first; re-verify at sidecar cutover."
            )

        chat_id = next(
            (c["id"] for c in chats.get("data", [])
             if isinstance(c, dict) and c.get("name") == CHAT_NAME),
            None,
        )
        if chat_id is None and chats.get("data"):
            print(f"existing chats: "
                  f"{[(c.get('name'), c.get('id')) for c in chats['data']]}")

        # -- S10e: create a chat via API -------------------------------------
        if chat_id is None:
            created = show(
                "S10e: POST create chat",
                await http.post(
                    f"/v1/spaces/{space_id}/chats", json={"name": CHAT_NAME}
                ),
            )
            if created:
                chat_id = (created.get("chat") or created.get("object") or {}).get(
                    "id"
                ) or created.get("id")
        if chat_id is None:
            sys.exit(
                "no usable chat (create failed and none named "
                f"{CHAT_NAME!r}); create one in the desktop app and rerun"
            )
        print(f"\nchat id: {chat_id}")

        # -- S10d: identity --------------------------------------------------
        show(
            "S10d: GET members (look for a self/identity marker)",
            await http.get(f"/v1/spaces/{space_id}/members", params={"limit": 50}),
        )
        for guess in ("/v1/auth/me", "/v1/me", f"/v1/spaces/{space_id}/me"):
            r = await http.get(guess)
            print(f"S10d: GET {guess} -> {r.status_code}"
                  + (f" {r.text[:200]}" if r.status_code == 200 else ""))

        # -- S10b: send + read messages --------------------------------------
        sent = show(
            "S10b: POST message (capture the 201 payload)",
            await http.post(
                f"/v1/spaces/{space_id}/chats/{chat_id}/messages",
                json={"text": "S10 probe: plain text with **markdown** and a "
                              "link [name](bafyexampleobjectid)"},
            ),
        )
        message_id = None
        if sent:
            message_id = (sent.get("message") or {}).get("id") or sent.get("id")
        show(
            "S10b: GET messages (payload schema: creator/text/order fields)",
            await http.get(
                f"/v1/spaces/{space_id}/chats/{chat_id}/messages",
                params={"limit": 5},
            ),
        )

        # -- S10f: length cap -------------------------------------------------
        long_text = "x" * 5000
        r = await http.post(
            f"/v1/spaces/{space_id}/chats/{chat_id}/messages",
            json={"text": long_text},
        )
        print(f"\nS10f: POST 5000-char message -> {r.status_code}"
              + ("" if r.status_code < 300 else f" {r.text[:300]}"))
        if r.status_code < 300:
            listing = (await http.get(
                f"/v1/spaces/{space_id}/chats/{chat_id}/messages",
                params={"limit": 1},
            )).json()
            got = (listing.get("data") or [{}])[-1]
            text_field = next(
                (v for v in got.values() if isinstance(v, str) and v.startswith("xxx")),
                "",
            )
            print(f"S10f: stored length (top-level string field): "
                  f"{len(text_field) if text_field else 'see raw above'}")

        # -- S10c: SSE stream --------------------------------------------------
        print("\nS10c: opening SSE stream (heartbeat 5s), posting one live "
              "message, capturing ~15s of frames...")

        async def poster() -> None:
            await asyncio.sleep(3)
            await http.post(
                f"/v1/spaces/{space_id}/chats/{chat_id}/messages",
                json={"text": "S10 live event probe"},
            )

        async def streamer() -> None:
            stream_headers = {"Anytype-Heartbeat-Seconds": "5"}
            try:
                async with http.stream(
                    "GET",
                    f"/v1/spaces/{space_id}/chats/{chat_id}/messages/stream",
                    headers=stream_headers,
                    timeout=httpx.Timeout(10.0, read=20.0),
                ) as response:
                    print(f"stream status: {response.status_code} "
                          f"content-type: {response.headers.get('content-type')}")
                    if response.status_code != 200:
                        return
                    async with asyncio.timeout(15):
                        async for line in response.aiter_lines():
                            print(f"SSE| {line[:500]}")
            except (TimeoutError, httpx.ReadTimeout):
                print("(stream window elapsed)")

        await asyncio.gather(streamer(), poster())

        # -- cleanup the probe messages (keep the chat for reruns) -------------
        if message_id:
            r = await http.delete(
                f"/v1/spaces/{space_id}/chats/{chat_id}/messages/{message_id}"
            )
            print(f"\ncleanup: DELETE first probe message -> {r.status_code}")

    print("\nDone. Record S10a-S10f in docs/WORK_PACKAGES.md (WP14).")


if __name__ == "__main__":
    asyncio.run(main())
