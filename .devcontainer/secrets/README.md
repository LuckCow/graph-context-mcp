# Secrets

Every file in this directory is git-ignored and mounted read-only into the
dev container at `/run/secrets/<name>` (see `docker-compose.yml`). App code
reads keys from these files, never from env vars (env vars leak via
`docker inspect`, `/proc`, and child processes).

| File | Used by | How to obtain |
|---|---|---|
| `anytype_api_key` | everything that talks to the Anytype API (`ANYTYPE_API_KEY_FILE`) | `docker exec -it graph-context-mcp-anytype anytype auth apikey create "graph-context"` and paste the key here (the sidecar bot account's key — the desktop pairing flow is not involved). |
| `discord_bot_token` | the Discord transport (`DISCORD_BOT_TOKEN_FILE`) | Discord developer portal → your bot → Reset Token. |
| `claude_oauth_token` | the Claude Code CLI driver (subscription auth) | `claude setup-token` on a logged-in machine. |
| `gc_prose_token` | the prose page's write gate (`GC_PROSE_TOKEN_FILE`, ADR 050/054) | Any random string you choose, e.g. `python -c "import secrets; print(secrets.token_urlsafe(24))"`; the `/prose` page asks for it once on the first save. |
| `anytype_account_key` | nothing at runtime — **disaster-recovery backup** of the sidecar bot account's identity | Written by hand from the output of `anytype auth create graph-context-bot` at sidecar bootstrap (WP14). Losing the sidecar's volumes without this file means losing the bot's identity and re-inviting it to every space. |
| `anthropic_api_key` | the Messages-API driver (`ANTHROPIC_API_KEY_FILE`, `GC_DRIVER=anthropic_api`) | [console.anthropic.com](https://console.anthropic.com) → API keys. **Leave empty to stay on the Claude subscription** — the billing gate treats an empty file as no key, so nothing bills API credits by accident. |
| `tailscale_authkey` | `start-tailscale.sh`, to join the tailnet at boot | Tailscale admin console → Settings → Keys → Generate auth key. Make it **reusable** (so rebuilds re-register), **non-ephemeral** (ephemeral nodes vanish when the container stops), and **tagged** (e.g. `tag:vps`) so ACLs can scope what the node reaches. |

**`anthropic_api_key` and `tailscale_authkey` must exist even if empty** — compose refuses to start when a
secret's file is missing. An empty file is the supported "this deployment has no
tailnet" configuration: `start-tailscale.sh` logs that it is skipping and the
container comes up exactly as it did before tailscale existed.

Rotation: replace the file content and restart the stack; nothing caches
keys beyond process lifetime.
