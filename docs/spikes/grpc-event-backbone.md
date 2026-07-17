# Research: anytype-heart's gRPC event stream as a push backbone

Feasibility notes gathered 2026-07-17, prompted by "can we learn about new
chats instantly instead of the rescan poll?". Decision for now: **not
pursued** — the rescan default dropped to 3s instead (sidecar reads are
unthrottled per S7, so a tight poll is near-instant and free). Kept here
because the same stream could later become a **general event backbone**:
instant new-chat discovery, instant out-of-band edit detection (replacing
the 60s graph-resync poll), rename detection, and potentially one
multiplexed connection instead of per-chat SSE streams.

Desk research only — verified against `anyproto/anytype-heart` and
`anyproto/anytype-cli` sources (main branches, 2026-07), **not yet run
against the sidecar**. Anything below marked *unverified* needs a live
spike before being relied on.

## What the heart exposes (verified in the protos)

- `ClientCommands` has exactly one streaming RPC
  (`pb/protos/service/service.proto`):
  `rpc ListenSessionEvents (StreamRequest) returns (stream Event)`.
- Relevant `Event.Message` variants (`pb/protos/events.proto`):
  - `Chat.Add` / `Chat.Update` / `Chat.Delete` / `Chat.UpdateState` —
    per-message chat traffic (we already get this via the JSON API's SSE).
  - `Object.Subscription.Add` / `.Remove` / `.Position` and
    `Object.Details.Set` / `.Amend` — fire for objects matching a prior
    `Rpc.Object.SearchSubscribe` query. **A subscription filtered to
    chat-layout objects per space is the instant "new chat created"
    signal.**
  - Each `Event.Message` carries `spaceId`; the envelope has `contextId`.
- Also present: `Rpc.Chat.SubscribeLastMessages`,
  `Rpc.Chat.SubscribeToMessagePreviews` — chat-message push over gRPC,
  an alternative to N per-chat SSE streams.
- Session auth RPCs: `WalletCreateSession` / `WalletCloseSession`,
  `AccountLocalLinkNewChallenge` / `AccountLocalLinkSolveChallenge`.

## The sidecar situation

- `anytype-cli serve` runs three listeners: gRPC **31010**, gRPC-Web
  **31011**, HTTP JSON API **31012** (the port set was chosen to coexist
  with the desktop app's 31007–31009).
- Our compose only reaches 31012: `--listen-address 0.0.0.0:31012`
  rebinds **the HTTP API only** (per anytype-cli's README, which suggests
  proxies/port-mapping for the rest), so gRPC presumably stays on
  loopback inside the container (*unverified* — check with
  `docker exec graph-context-mcp-anytype ss -tlnp`). No ports are
  published; the dev container's firewall allowlists only the compose
  subnet + host 31009/31012.
- Workaround if loopback-bound: a socat forward inside the sidecar
  container (0.0.0.0:31010 → 127.0.0.1:31010) plus compose wiring.

## Cost breakdown (why it's a full WP, not an afternoon)

1. **Port exposure** — small; Dockerfile/compose/firewall touch-ups, maybe
   socat (above).
2. **Dependency stack** — the repo has zero gRPC/protobuf machinery. Needs
   `grpcio` + `protobuf` added to the container (egress firewall: user
   adds them to the Docker setup) and generated stubs vendored from the
   heart's proto tree (`service.proto`, `events.proto`, and their
   `models.proto` import chain — large but mechanical; pin to the heart
   version the sidecar's anytype-cli embeds).
3. **Auth — the main open risk.** `ListenSessionEvents` wants a session
   token minted by `WalletCreateSession`. Its request accepts an **app
   key**; *plausibly* the existing `anytype_api_key` (minted by
   `anytype auth apikey create`) is accepted there — *unverified*.
   Fallback: the bot account key, which today lives **only** in the
   sidecar's `anytype-config` volume; the documented
   `.devcontainer/secrets/anytype_account_key` backup file is absent from
   the checkout and would need re-exporting.
4. **Integration** — a listener in `infrastructure/anytype` behind a
   narrow port; the bot consumes it as an *accelerator* that triggers an
   immediate rescan/resync, with the existing polls retained as the
   reconnect-safe fallback. Contract tests need a fake event source
   (`MockAnytype` is HTTP-only).

## If/when pursued

Spike first (S-numbered, results recorded in WORK_PACKAGES.md like
S10/S13): confirm the 31010 bind address, codegen minimal stubs, try
`WalletCreateSession(app_key=<existing api key>)` →
`ListenSessionEvents` + `ObjectSearchSubscribe(layout == chat)`, create a
chat in the UI, and time the `Object.Subscription.Add` arrival. Only if
that is clean: ADR + WP for the backbone integration.
