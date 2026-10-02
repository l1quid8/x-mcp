# Owner connection dashboard

Each self-hosted X MCP server has an owner dashboard at
`https://YOUR-ORIGIN/x-mcp/connect` (replace `/x-mcp` if you set a custom
`X_MCP_PREFIX`). Open it in your normal browser and enter the server owner
key. The dashboard shows Buffer channels and saved direct X sessions. It does
not present a VPS browser sign-in.

Buffer and direct X sessions serve different needs:

- **Buffer:** connect an X channel inside Buffer, then give X MCP a Buffer API
  key. This is enough for Buffer publishing; no X session import is required.
- **Optional direct X session:** connect from a Brave/Chromium profile already
  signed in to X using the local browser extension. The saved session supports
  direct publishing, session-based cleanup, and the same-account fallback when
  Buffer definitely reaches its API request limit.

Connecting either route does not give an MCP client permission to use it. The
client must receive the account and scope grants described below.

## Self-hosted deployment

Follow the [installation steps](../README.md#install-and-configure) first.
Use your own HTTPS hostname and set `X_MCP_ORIGIN` to that origin with no path.
The reverse proxy must forward the configured MCP prefix and OAuth discovery
paths to the loopback app listener. For the default prefix, a minimal Nginx
example is:

```nginx
location /x-mcp/ {
    proxy_pass http://127.0.0.1:8770;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_read_timeout 190s;
    access_log off;
}
location ^~ /.well-known/oauth-authorization-server/x-mcp/ {
    proxy_pass http://127.0.0.1:8770;
    proxy_set_header Host $host;
    access_log off;
}
location ^~ /.well-known/oauth-protected-resource/x-mcp/ {
    proxy_pass http://127.0.0.1:8770;
    proxy_set_header Host $host;
    access_log off;
}
```

The proxy needs a valid HTTPS certificate for `X_MCP_ORIGIN`. Keep the Python
listener on loopback. Docker Compose still runs a private browser worker for
compatibility. It has no published port or access to the app's persistent
private volume, uses a temporary filesystem, and shares a random token with
the app from the private `.env` file. It is not part of the recommended
account connection flow. Do not expose its internal ports.

## Connect Buffer

1. In your own browser, [connect X to Buffer](https://account.buffer.com/channels).
2. Create a key in [Buffer Settings → API](https://publish.buffer.com/settings/api)
   with `accountRead`, `postsRead`, and `postsWrite` only.
3. Open the X MCP owner dashboard, select **Manage Buffer**, and choose
   **Verify and save key**. X MCP verifies the key, lists usable X channels,
   and stores the key encrypted on your server. No X password or X website
   cookies pass through this Buffer setup.
4. If you reconnect a Buffer channel to a different X account, select
   **Refresh channels** before posting. Use the collapsed **Replace API key**
   section only when the key itself changes.

The Buffer page also has an optional backup switch: **Use a matching direct X
session if Buffer reaches its API limit**. It applies only to immediate
(`shareNow`) posts after a definite Buffer API quota rejection. A direct X
session for the *same verified numeric X account ID* must already be saved.
Queued or scheduled posts, Buffer posting limits, timeouts, server errors, and
unclear outcomes do not use this fallback. See the
[full fallback rules](../README.md#optional-direct-x-fallback).

## Connect an optional direct X session

Use the [browser extension instructions](../browser-extension/README.md) to
generate an extension pinned to your own HTTPS origin and, if needed, custom
path prefix. Load the generated extension into the Brave/Chromium profile
where you are already signed in to X. Select the account, approve its one-time
code using your server owner key, and wait for X MCP to verify the X account
identity before saving the selected session cookies encrypted on your server.
The extension is needed only to connect or renew the session; it does not need
to remain active for normal publishing. X can expire or restrict that session,
so reconnect it if a publication receipt reports that it no longer works.

This direct session is website-session authentication through Twikit, not
official X OAuth. The checked-in extension template points to an invalid
example domain and cannot send a session to a real server. Generate a private
copy for your deployment and keep your owner key and session material out of
the repository.

## Grant an MCP client access

After connecting accounts, approve each client and the accounts and scopes it
needs. Buffer publishing requires `buffer:publish` for the selected
`buffer:<channel_id>`. Direct publishing requires `publisher:publish` for the
matching numeric X account ID; direct image publishing also requires
`publisher:media`. The optional fallback requires **both** routes to be
granted. An existing Buffer-only grant does not gain direct-session access,
and a newly connected account does not appear in an existing grant
automatically. Reconnect the MCP client and approve the new access.

If Codex or ChatGPT reports that its callback is not allowed, use the separate
owner-only `/x-mcp/connect/client-settings` page to allow the exact callback
URL shown by that client, then retry. Callback approval is for MCP client
authorization; it does not connect an X account. The current server is
single-owner. Do not share its owner key or present this as a multi-user hosted
service.
