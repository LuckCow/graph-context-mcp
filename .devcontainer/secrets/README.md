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
| `anytype_account_key` | nothing at runtime — **disaster-recovery backup** of the sidecar bot account's identity | Written by hand from the output of `anytype auth create graph-context-bot` at sidecar bootstrap (WP14). Losing the sidecar's volumes without this file means losing the bot's identity and re-inviting it to every space. |

Rotation: replace the file content and restart the stack; nothing caches
keys beyond process lifetime.
