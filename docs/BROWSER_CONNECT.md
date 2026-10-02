# Browser-only account connection

X MCP's `/x-mcp/connect` page lets the server owner connect an X account from
any normal browser. The X browser itself runs on the self-hosted VPS. The owner
signs in there, then X MCP verifies the account and stores only the selected X
session cookies in its encrypted private database. The temporary browser closes
after connection, cancellation, or 15 minutes. No browser extension or local
helper is needed on the owner's computer.

This is session authentication for publishing through Twikit, not official X
OAuth. MCP client authorization remains a separate browser-based OAuth grant.
An existing MCP client grant does not acquire a newly connected account.

## Deployment

Use the repository's `docker/configure.py` and `compose.yaml` for new installs.
The app container stores keys and state in the `x_mcp_private` volume. The
browser container has no published port, no access to that volume, and a
temporary filesystem. Both services must use the same random
`X_MCP_BROWSER_WORKER_TOKEN` from the private `.env` file.

The HTTPS proxy must forward the MCP path, OAuth discovery paths, and WebSocket
upgrades. With the default path prefix, a minimal Nginx example is:

```nginx
location = /x-mcp/connect/ws {
    proxy_pass http://127.0.0.1:8770;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 900s;
    access_log off;
}
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

The proxy must have a valid HTTPS certificate for `X_MCP_ORIGIN`. Keep the
Python port bound to loopback and do not route the browser worker's internal
ports to the public internet. If using a manual Python service instead of
Compose, set `X_MCP_BROWSER_WORKER_URL`, `X_MCP_BROWSER_VIEW_URL`, and a shared
random `X_MCP_BROWSER_WORKER_TOKEN` in that service; run the browser worker on
a private network or bind its ports to loopback only.

## What the owner sees

1. Open `https://YOUR-ORIGIN/x-mcp/connect` on the local computer.
2. Enter the X MCP owner key. The page shows existing connected accounts.
3. Choose **New account** or an account to reconnect, then **Open X sign-in**.
4. Sign in to X in the browser view. Complete any X verification there.
5. After the X home feed appears, choose **Finish connection**. X MCP checks
   the authenticated account ID before storing the encrypted session.
6. Connect the MCP client and approve that account on the separate MCP consent
   page. If the client says its callback is not allowed, open
   `/x-mcp/connect/client-settings`, enter the owner key, and allow the exact
   callback URL shown by the client. Then retry. Publishing uses the saved
   session; the temporary browser can close.

If X rejects login from the VPS or requires a passkey stored only on the local
computer, use another X verification method or the optional local connection
helper. Repeated login attempts can trigger X account restrictions, so resolve
the challenge in X before retrying. The browser service opens X's login page;
X MCP does not ask for the X password in its own form.

The current server is single-owner. Do not share its owner key with other
people or present this flow as a multi-user hosted service.
