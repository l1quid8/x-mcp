# X MCP

X MCP is a **self-hosted** Streamable HTTP MCP server for reading public X posts,
publishing to accounts you connect, and reviewing or deleting your own posts.
Reading, publishing, and cleanup share one server, OAuth resource, and account
permission system. This repository provides source code; it does **not** provide
a hosted MCP endpoint, account sessions, or credentials.

The reader adapts the MIT-licensed [Nitter MCP](https://github.com/Alastrantia/nitter-mcp)
code and currently requests RSS from public Nitter instances. Publishing can
use [Buffer](https://buffer.com/api), with X connected to Buffer in your local
browser, or a direct X session through the MIT-licensed
[Twikit](https://github.com/d60/twikit) library. Session-based cleanup still
uses Twikit. See [provenance and license credits](docs/PROVENANCE.md). This is an
integrated derivative project, not an independent implementation of those
components or a copy of X's data.

## What you can do

- **Read:** search public posts, fetch a public account timeline, combine recent
  posts from curated news sources, and inspect mirror freshness.
- **Publish through Buffer:** preview an exact text or image post, send it now,
  queue it, or schedule it for a connected X channel, then check Buffer's
  delivery status. Image URLs must be direct, public, and remain available
  until Buffer sends the post.
- **Optional direct fallback:** after a definite Buffer API quota rejection,
  immediately publish a `shareNow` post through an authorized direct X session
  for the same X account. The receipt identifies the route actually used.
- **Publish through a direct X session:** stage media, preview an exact post or
  thread, submit it to a selected account, and check the operation receipt.
  Article publishing is disabled pending protocol validation.
- **Clean up:** scan owned content, protect selected posts, review a frozen
  deletion plan, dry run it, and execute an explicitly requested plan.

Buffer publishing permissions are bound to selected `buffer:<channel_id>`
destinations. Direct-session publishing and cleanup permissions are bound to
numeric X account IDs. A read grant cannot authorize a post or deletion. Post
text, search results, and cleanup reasons are treated as untrusted data.

## Requirements

- Docker with Compose for a self-hosted setup, or Python 3.11+ and
  [uv](https://docs.astral.sh/uv/) for a manual installation.
- Your own HTTPS hostname and reverse proxy. The Python server listens only on
  `127.0.0.1:8770` by default; the proxy must forward your chosen path prefix.
- Persistent private state, an encryption key, and an owner key. Never commit
  keys, OAuth tokens, X sessions, or staged media.
- For Buffer publishing, your own Buffer account with its X channel connected
  and a Buffer API key. A direct X session is needed only for the optional
  session publisher or session-based cleanup. Public reading currently also
  needs reachable Nitter instances.

The examples below use `mcp.example.com`, a placeholder. Replace it with a
hostname you control. **There is no default remote destination:** the server
and account helpers refuse to start without `X_MCP_ORIGIN`.

## Install and configure

### Self-hosted setup with Docker Compose

This runs X MCP and its private support services on your VPS. The owner
dashboard uses your normal browser. Buffer publishing does not require the
browser extension. The recommended direct X connection method uses the
extension for direct publishing, cleanup, or the optional fallback.

```sh
git clone https://github.com/l1quid8/x-mcp.git
cd x-mcp
python3 docker/configure.py https://mcp.your-domain.com
docker compose up -d --build
docker compose exec app cat /var/lib/x-mcp/keys/owner-key
```

Save the owner key privately. The setup command creates a private `.env` with a
random browser-worker token; both files are excluded from Git. Compose still
includes a private browser worker for compatibility, but the owner dashboard
does not offer VPS browser sign-in. Compose stores encrypted sessions and keys
in a persistent private volume. The only published container port is
`127.0.0.1:8770`; place your own HTTPS reverse proxy in front of it. Forward
`/x-mcp/*` and the OAuth discovery paths under
`/.well-known/oauth-authorization-server/x-mcp/*` and
`/.well-known/oauth-protected-resource/x-mcp/*`. Do not publish the browser
service's ports. See [owner connection details](docs/BROWSER_CONNECT.md) for a
proxy example.

For Buffer publishing, connect X at [Buffer's channel settings](https://account.buffer.com/channels),
then open `https://mcp.your-domain.com/x-mcp/connect` on your own computer.
Enter your server owner key, choose **Manage Buffer**, and save a Buffer API
key. The server checks which X channels that key can use and stores the key
encrypted. This Buffer setup does not pass an X password or website cookies
through X MCP. [Buffer offers API access on its Free
plan](https://buffer.com/pricing), subject to its channel, queue, and request
limits. Each self-hosted deployment needs its own Buffer account and key.

### Manual Python service

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
sudo useradd --system --home-dir /var/lib/x-mcp --shell /usr/sbin/nologin xmcp
sudo env X_MCP_ORIGIN=https://mcp.example.com /opt/x-mcp/.venv/bin/x-mcp-admin init
sudo install -d -o xmcp -g xmcp -m 0700 /var/lib/x-mcp/reader
sudo chown -R xmcp:xmcp /var/lib/x-mcp
```

The admin command requires root. By default it stores keys under `/etc/x-mcp`
and SQLite/media state under `/var/lib/x-mcp`. Use `CREDENTIALS_DIRECTORY` and
`X_MCP_STATE_DIR` if your host uses different private paths. The service user
must be able to write its state and reader-cache directories. Admin commands
restore state ownership to `xmcp` by default; set `X_MCP_SERVICE_USER` when
your service runs as a different user. A systemd service can pass the keys
through `LoadCredential`:

```ini
[Service]
User=xmcp
Group=xmcp
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

Open `https://mcp.your-domain.com/x-mcp/connect` in your own browser and sign
in with the server owner key. This is the owner dashboard: it shows your Buffer
channels and saved direct X sessions in one place. **Manage Buffer** opens the
Buffer setup page. A direct X session is optional, and the dashboard does not
offer VPS browser sign-in.

### Buffer publishing

Create a personal key in [Buffer Settings → API](https://publish.buffer.com/settings/api).
Select `accountRead`, `postsRead`, and `postsWrite` permissions; leave unrelated
permissions off. Keep the key private. From the owner dashboard, select
**Manage Buffer** and **Verify and save key**. Once saved, the page lists the
usable X channels verified by Buffer. Use **Refresh channels** after changing
which X account is connected inside Buffer. The API key field stays collapsed
under **Replace API key** unless you need to change it. Connecting a channel
does not grant an MCP client permission to publish; approve that channel and
`buffer:publish` separately in
the MCP consent page. For manual server administration, a root-only
`x-mcp-admin configure-buffer --file /path/to/private-key-file` command is
available; the input file must be mode `0600`.

Buffer's API takes image URLs rather than file uploads. A URL must load the
image directly over public HTTPS without login and remain live until the post
publishes. [Buffer explains media hosting requirements](https://developers.buffer.com/guides/hosting-media.html).
`shareNow` asks Buffer to publish immediately; a successful Buffer API response
can still mean queued or processing. Check `buffer_post_status` before reporting
delivery to X. This integration does not turn Buffer into a general X search or
cleanup API.

#### Optional direct X fallback

The server owner can enable **Use a matching direct X session if Buffer reaches
its API limit** on the Buffer connection page. This backup uses an optional
direct X session already connected to the server; the extension is needed only
when connecting or renewing that session, not while posts are being sent.
Buffer's verified X account ID must match the numeric ID of a connected direct
X session. The MCP client must be granted `buffer:publish` for the Buffer channel
and `publisher:publish` for that direct X account. Image posts also require
`publisher:media`. An existing Buffer-only grant does not gain direct-session
access; reconnect the MCP client and approve both accounts and permissions.
If you reconnect a channel to a different X account inside Buffer, use
**Refresh channels** on the owner page or run `x-mcp-admin sync-buffer-channels`
before relying on fallback. A Buffer key or
verified account change invalidates existing post previews.

For `shareNow` posts, a definite Buffer API quota rejection (HTTP 429 or its
equivalent GraphQL code) can trigger one direct X attempt without another
prompt. This does not apply to queued or scheduled posts, Buffer posting/queue
limits, timeouts, server errors, or unclear responses. Those outcomes stop so
the same post is not sent twice. Direct fallback still depends on a working X
session and may fail if X restricts it. Public image URLs are downloaded to
private staging, with a 5 MiB cap per image, before direct publication. The
operation receipt names the provider and gives the direct X post result.
[Buffer documents its API limits and 429 response](https://developers.buffer.com/guides/api-limits.html).

### Optional direct X session and cleanup

For direct publishing, session-based cleanup, or the Buffer API-limit fallback,
connect a direct X session using the extension in the browser profile where
you are already signed in to X. The extension transfers selected session
cookies to your own server after one-time owner approval. It does not need to
stay active for publishing. If X expires or rejects the saved session, connect
it again. No direct X session is needed for Buffer-only publishing.

The [browser extension template](browser-extension/README.md) can be generated
for your exact server origin, then loaded as an unpacked Brave/Chromium
extension. Enter the X handle signed into that browser profile, approve the
one-time code with your server's owner key, and wait for server-side identity
verification. The checked-in template points only to an invalid example domain
and cannot send a session to a real server. An optional
[local account helper](connect/README.md) is also available to operators who
prefer a separate local browser session; it requires your configured origin.

Connecting an account stores its X session encrypted on your server; it does
not publish a post or grant an MCP client permission to use that account. Use
`x-mcp-admin accounts` to inspect connected IDs. For a direct client credential,
the root-only `x-mcp-admin issue-client` command writes a private connection
file with your resource URL and selected scopes. For OAuth clients, allow their
exact callback in the owner-only `/x-mcp/connect/client-settings` page or with
`x-mcp-admin allow-callback`, then approve the requested accounts and sensitive
scopes on your consent page.

## Connect ChatGPT on the web

This flow creates a personal ChatGPT plugin entry from the MCP URL. It does not
require a plugin archive or Plugin Creator. Your ChatGPT account or workspace
must have Developer mode for custom MCP servers enabled.

1. On the MCP server, allow ChatGPT's stable OAuth callback once:

   ```sh
   x-mcp-admin allow-callback https://chatgpt.com/connector_platform_oauth_redirect
   ```

   You can also enter that URL on the owner-only
   `/x-mcp/connect/client-settings` page. This approves the callback destination,
   not access to an X account or permission to publish.
2. In ChatGPT, enable Developer mode under **Settings → Security and login**.
   Open **Plugins → Add → Create custom MCP server** and enter your public HTTPS
   endpoint, for example `https://mcp.example.com/x-mcp/mcp`. Choose OAuth.
   For the optional icon, upload the included
   [`x-mcp-icon.png`](src/x_publisher/assets/x-mcp-icon.png). It is a verified
   1280 × 1280 RGB PNG. A `.png` filename alone does not make an image a
   decodable PNG. Select every listed scope under **Default scopes** to enable the
   complete publishing, Buffer, media, and cleanup workflow. Set
   `X_MCP_DEFAULT_SCOPES=all` on the server if every new client should request the
   complete set by default.
3. Complete the server's owner consent page. Select the account and permissions
   you want this ChatGPT connection to have, then let ChatGPT scan the tools.

The server advertises OAuth issuer identification, so new ChatGPT connections
use the stable callback above. Older connections that show a distinct
`https://chatgpt.com/connector/oauth/...` callback still need that exact URL
approved in client settings. Keep the owner key private; the callback allowlist
alone never grants access.

## Connect Codex

After your server is reachable over HTTPS, add its MCP resource URL to Codex:

```sh
codex mcp add x-mcp --url https://mcp.example.com/x-mcp/mcp
```

Codex CLI, the IDE extension, and the ChatGPT desktop app share this local MCP
configuration. The server requires authentication. You can use OAuth with
`codex mcp login x-mcp --oauth-client-registration dcr`; the server owner must
first approve the Codex callback path in `/x-mcp/connect/client-settings` or with
`x-mcp-admin allow-callback`. Codex
uses a `127.0.0.1` callback whose port can change between logins, so approval
is tied to its exact `/callback/...` path. The consent page then lets the owner
grant only the requested scopes and connected accounts. New grants request
`x:read` by default; set `X_MCP_DEFAULT_SCOPES=all` to request every available
scope. The owner still selects accounts and explicitly approves sensitive
publishing and cleanup permissions during consent.

For local clients using a direct credential, keep the token in a private file
outside the repository and supply it through Codex's `http_headers_helper` or
an environment variable. Do not put bearer tokens in a public config or commit
them to this repository. Keep write-tool approval prompts enabled in your
client. See the [Codex MCP configuration guide](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

## Tools and permissions

| Permission | Tools | Purpose |
| --- | --- | --- |
| `x:read` | `search_x`, `get_user_posts`, `get_breaking_news`, `reader_status` | Public reading and mirror diagnostics |
| `publisher:status` | `publishing_status`, `publication_status` | Authorized accounts and operation receipts |
| `publisher:media` | `stage_media`, `begin_media_upload` | Private media staging |
| `publisher:publish` | `preview_publication`, `publish_publication` | Exact draft preview and publication |
| `buffer:status` | `buffer_status`, `buffer_post_status` | Connected Buffer X channels and delivery receipts |
| `buffer:publish` | `preview_buffer_post`, `publish_buffer_post` | Preview and submit exact Buffer posts |
| `cleanup:read` | `cleanup_status`, `scan_content`, `deletion_status`, `deletion_audit_history`, `cleanup_protections` (read) | Owned-content scans and review |
| `cleanup:plan` | `stage_deletion_actions`, `preview_deletion_plan` | Frozen deletion plans |
| `cleanup:execute` | `execute_deletion_plan` | Dry run or execute an exact plan |
| `cleanup:protect` | `cleanup_protections` (update) | Update deletion protections |

New OAuth grants request `x:read` unless `X_MCP_DEFAULT_SCOPES=all` is set.
Publishing and cleanup grants
require selected account IDs, and sensitive scopes need explicit approval.
Buffer grants select `buffer:<channel_id>` destinations separately from direct
X session accounts. Existing direct-session tokens do not gain Buffer access.
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
not validate live Buffer or X publishing, or account imports. Article publishing is
disabled. Public reads depend on third-party mirrors and may be incomplete or
stale despite health checks. The session-backed write and cleanup adapters use
unofficial X interfaces and can be affected by account restrictions or X
changes. The Buffer publisher depends on Buffer's API and its connection to X;
it cannot read arbitrary X content or upload private image files. No production
deployment is included in this repository.

The Python wheel includes the adapted reader's MIT license. Twikit remains a
separate pinned dependency with its own MIT license. Keep their notices and the
[provenance record](docs/PROVENANCE.md) when redistributing this project.
