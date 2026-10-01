# X MCP

X MCP is a **self-hosted** Streamable HTTP MCP server for reading public X posts,
publishing to accounts you connect, and reviewing or deleting your own posts.
Reading, publishing, and cleanup share one server, OAuth resource, and account
permission system. This repository provides source code; it does **not** provide
a hosted MCP endpoint, account sessions, or credentials.

The reader adapts the MIT-licensed [Nitter MCP](https://github.com/Alastrantia/nitter-mcp)
code and currently requests RSS from public Nitter instances. Publishing and
session-based cleanup use the MIT-licensed [Twikit](https://github.com/d60/twikit)
library. See [provenance and license credits](docs/PROVENANCE.md). This is an
integrated derivative project, not an independent implementation of those
components or a copy of X's data.

## What you can do

- **Read:** search public posts, fetch a public account timeline, combine recent
  posts from curated news sources, and inspect mirror freshness.
- **Publish:** stage media, preview an exact post or thread, submit it to a
  selected connected account, and check the operation receipt. Article
  publishing is disabled pending protocol validation.
- **Clean up:** scan owned content, protect selected posts, review a frozen
  deletion plan, dry run it, and execute an explicitly requested plan.

Publishing and cleanup permissions are bound to numeric X account IDs. A read
grant cannot authorize a post or deletion. Post text, search results, and
cleanup reasons are treated as untrusted data.

## Requirements

- Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).
- Your own HTTPS hostname and reverse proxy. The Python server listens only on
  `127.0.0.1:8770` by default; the proxy must forward your chosen path prefix.
- Persistent private state, an encryption key, and an owner key. Never commit
  keys, OAuth tokens, X sessions, or staged media.
- An X session for each account you want to publish from or clean up. Public
  reading currently also needs reachable Nitter instances.

The examples below use `mcp.example.com`, a placeholder. Replace it with a
hostname you control. **There is no default remote destination:** the server
and account helpers refuse to start without `X_MCP_ORIGIN`.

## Install and configure

```sh
git clone https://github.com/l1quid8/x-mcp.git
cd x-mcp
uv sync --locked --no-dev
export X_MCP_ORIGIN=https://mcp.example.com
```

`X_MCP_ORIGIN` is the public HTTPS origin **without** a path, query, or
credentials. `X_MCP_PREFIX` defaults to `/x-mcp`. With the example settings,
your MCP resource would be `https://mcp.example.com/x-mcp/mcp`. Each deployment
must use its own origin so OAuth audiences and cookie-transfer destinations
cannot point to someone else's server.

For a Linux service, install the reviewed checkout and virtual environment in
a root-owned location such as `/opt/x-mcp`. Create a dedicated service user and
initialize private keys and state once:

```sh
sudo useradd --system --home-dir /var/lib/x-mcp --shell /usr/sbin/nologin xpublisher
sudo env X_MCP_ORIGIN=https://mcp.example.com \
  X_MCP_SERVICE_USER=xpublisher \
  /opt/x-mcp/.venv/bin/x-mcp-admin init
```

The admin command requires root. By default it stores keys under `/etc/x-mcp`
and SQLite/media state under `/var/lib/x-mcp`. Use `CREDENTIALS_DIRECTORY` and
`X_MCP_STATE_DIR` if your host uses different private paths. The service user
must be able to write its state and reader-cache directories. A systemd service
can pass the keys through `LoadCredential`:

```ini
[Service]
User=xpublisher
Group=xpublisher
WorkingDirectory=/opt/x-mcp
ExecStart=/opt/x-mcp/.venv/bin/x-mcp
Environment=X_MCP_ORIGIN=https://mcp.example.com
Environment=X_MCP_STATE_DIR=/var/lib/x-mcp
Environment=X_MCP_READER_STATE_DIR=/var/lib/x-mcp/reader
LoadCredential=encryption-key:/etc/x-mcp/encryption-key
LoadCredential=owner-key:/etc/x-mcp/owner-key
```

Put an HTTPS reverse proxy in front of the loopback server and route `/x-mcp/*`
to it. If you set a different `X_MCP_PREFIX`, use that path consistently in the
proxy and browser connector. Keep the Python listener private. This repository
does not install a service or change an existing deployment.

| Setting | Purpose |
| --- | --- |
| `X_MCP_ORIGIN` | **Required** public HTTPS origin for this installation |
| `X_MCP_PREFIX` | MCP, OAuth, pairing, and upload path prefix; default `/x-mcp` |
| `X_MCP_PORT` | Local listener port; default `8770` |
| `CREDENTIALS_DIRECTORY` | Directory containing `encryption-key` and `owner-key` |
| `X_MCP_STATE_DIR` | Private SQLite database and staged media |
| `X_MCP_READER_STATE_DIR` | Reader health and cache state |
| `X_MCP_READER_INSTANCES` | Optional comma-separated public Nitter mirror URLs |

More reader settings and freshness behavior are in [reader notes](docs/READER.md).
Configured mirror URLs must be public HTTPS hosts; the reader blocks private and
loopback targets to prevent server-side request forgery.

## Connect an account

The [browser extension template](browser-extension/README.md) can be generated
for your exact server origin, then loaded as an unpacked Brave/Chromium
extension. Enter the X handle signed into that browser profile, approve the
one-time code with your server's owner key, and wait for server-side identity
verification. The checked-in template points only to an invalid example domain
and cannot send a session to a real server. The optional
[local account helper](connect/README.md) also requires your configured origin.

Connecting an account stores its X session encrypted on your server; it does
not publish a post or grant an MCP client permission to use that account. Use
`x-mcp-admin accounts` to inspect connected IDs. For a direct client credential,
the root-only `x-mcp-admin issue-client` command writes a private connection
file with your resource URL and selected scopes. For OAuth clients, allow their
exact callback with `x-mcp-admin allow-callback` and approve the requested
accounts and sensitive scopes on your consent page.

## Tools and permissions

| Permission | Tools | Purpose |
| --- | --- | --- |
| `x:read` | `search_x`, `get_user_posts`, `get_breaking_news`, `reader_status` | Public reading and mirror diagnostics |
| `publisher:status` | `publishing_status`, `publication_status` | Authorized accounts and operation receipts |
| `publisher:media` | `stage_media`, `begin_media_upload` | Private media staging |
| `publisher:publish` | `preview_publication`, `publish_publication` | Exact draft preview and publication |
| `cleanup:read` | `cleanup_status`, `scan_content`, `deletion_status`, `deletion_audit_history`, `cleanup_protections` (read) | Owned-content scans and review |
| `cleanup:plan` | `stage_deletion_actions`, `preview_deletion_plan` | Frozen deletion plans |
| `cleanup:execute` | `execute_deletion_plan` | Dry run or execute an exact plan |
| `cleanup:protect` | `cleanup_protections` (update) | Update deletion protections |

New OAuth grants default to `x:read` only. Publishing and cleanup grants
require selected account IDs, and sensitive scopes need explicit approval.
For publishing, preview freezes the exact content; submit its `draft_id` with
a stable idempotency key only on an explicit request to publish. Poll
`publication_status` for a receipt. A queued operation is not a successful
post, and an unknown outcome must be reconciled before trying again. Cleanup
execution defaults to a dry run and requires an explicit request for the
reviewed targets and account.

## Verify and current limits

```sh
uv sync --locked --dev
uv run pytest -q
node --test tests/extension/protocol.test.mjs
uv build
```

The tests use a reserved example origin and mocked network responses. They do
not validate live X publishing or account imports. Article publishing is
disabled. Public reads depend on third-party mirrors and may be incomplete or
stale despite health checks. The session-backed write and cleanup adapters use
unofficial X interfaces and can be affected by account restrictions or X
changes. No production deployment is included in this repository.

The Python wheel includes the adapted reader's MIT license. Twikit remains a
separate pinned dependency with its own MIT license. Keep their notices and the
[provenance record](docs/PROVENANCE.md) when redistributing this project.
