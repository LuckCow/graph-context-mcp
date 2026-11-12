# Secrets

Every file in this directory is git-ignored and mounted read-only into the
dev container at `/run/secrets/<name>` (see `docker-compose.yml`). App code
reads keys from these files, never from env vars (env vars leak via
`docker inspect`, `/proc`, and child processes).

| File | Used by | How to obtain |
|---|---|---|
| `anytype_api_key` | everything that talks to the Anytype API (`ANYTYPE_API_KEY_FILE`) | `docker exec -it graph-context-mcp-anytype anytype auth apikey create "graph-context"` and paste the key here (the sidecar bot account's key — the desktop pairing flow is not involved). |
| `discord_bot_token` | the Discord transport (`DISCORD_BOT_TOKEN_FILE`) | Discord developer portal → your bot → Reset Token. |
| `claude_oauth_token` | the subscription driver (`CLAUDE_OAUTH_TOKEN_FILE`, `GC_DRIVER=anthropic_subscription`) | `claude setup-token` on a logged-in machine; `scripts/gc-prod token` installs it on the server. **Leave empty** to authenticate from the stored `claude` login in the `claude-config` volume instead. |
| `gc_prose_token` | the prose page's write gate (`GC_PROSE_TOKEN_FILE`, ADR 050/054) | Any random string you choose, e.g. `python -c "import secrets; print(secrets.token_urlsafe(24))"`; the `/prose` page asks for it once on the first save. |
| `anytype_account_key` | nothing at runtime — **disaster-recovery backup** of the sidecar bot account's identity | Written by hand from the output of `anytype auth create graph-context-bot` at sidecar bootstrap (WP14). Losing the sidecar's volumes without this file means losing the bot's identity and re-inviting it to every space. |
| `anthropic_api_key` | the Messages-API driver (`ANTHROPIC_API_KEY_FILE`, `GC_DRIVER=anthropic_api`) | [console.anthropic.com](https://console.anthropic.com) → API keys. **Leave empty to stay on the Claude subscription** — the billing gate treats an empty file as no key, so nothing bills API credits by accident. |
| `tailscale_authkey` | `start-tailscale.sh`, to join the tailnet at boot | Tailscale admin console → Settings → Keys → Generate auth key. Make it **reusable** (so rebuilds re-register), **non-ephemeral** (ephemeral nodes vanish when the container stops), and **tagged** (e.g. `tag:vps`) so ACLs can scope what the node reaches. |

**`anthropic_api_key`, `claude_oauth_token` and `tailscale_authkey` must exist even if empty** — compose refuses to start when a
secret's file is missing. An empty file is the supported "this deployment has no
tailnet" configuration: `start-tailscale.sh` logs that it is skipping and the
container comes up exactly as it did before tailscale existed.

Rotation: replace the file content and restart the stack; nothing caches
keys beyond process lifetime. Write the new content INTO the existing file
(`cat > file`, or an editor that saves in place): each secret is a
single-file bind mount, which follows the inode, so a file that was replaced
by a rename stays invisible to the running container until it is recreated.

## Its non-secret sibling

`spaces.toml` at the repo root is git-ignored for a related but different
reason ([ADR 060](../../docs/adr/060-deployment-scoped-space-bindings.md)): it
is not a secret — a space id is just an identifier — but it is
**deployment-scoped**, one copy per host, naming the Anytype spaces *this*
deployment serves. Since the dev box and the VPS share one bot account, that
file is the only thing keeping two orchestrators off the same chats, which it
cannot be if git carries it between them. A missing one is minted from a
packaged template on first boot, with instructions inside. `channels.toml` (the
Discord equivalent) is the same, except that absent simply means "Discord
parked" and nothing is minted.
