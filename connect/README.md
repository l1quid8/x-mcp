# Optional local account helper

`connect_x.py` is an alternative to the browser extension. It opens a fresh
Playwright browser session, asks you to sign into an owned X account, then uses
an owner-approved one-time pairing grant to transfer only the selected X cookies
to **your own** X MCP server. Install Playwright and its browser separately.

Set `X_MCP_ORIGIN` to your server's HTTPS origin before running it. The helper
has no default destination and refuses redirects during cookie transfer.

```sh
export X_MCP_ORIGIN=https://mcp.example.com
python connect/connect_x.py --account your_handle --tier unknown
```

Replace the example host and account. For a custom server path, also set
`X_MCP_PREFIX`. Optional SSH pairing needs `--pairing-method ssh --ssh-user USER`;
set `X_MCP_SSH_PORT` and `X_MCP_ADMIN_PATH` if your host differs from the defaults.
HTTPS pairing is the default and needs no SSH access.

The helper does not publish a post. Keep server keys, pairing grants, and X
sessions outside this repository. The [browser extension](../browser-extension/README.md)
is preferable when you want to use an existing signed-in browser profile.
