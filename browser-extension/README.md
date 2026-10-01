# Self-hosted X account connector

The checked-in extension is an inert template pinned to `mcp.example.invalid`.
Generate a copy for **your own** HTTPS server before loading it in Brave or
Chromium:

```sh
python browser-extension/configure.py \
  --origin https://mcp.example.com \
  --output "$HOME/x-mcp-extension"
```

Replace `mcp.example.com` with the same origin used for `X_MCP_ORIGIN`. If the
server uses a custom `X_MCP_PREFIX`, also pass `--prefix`. The generator rejects
URLs with paths or credentials and refuses to overwrite an existing directory.
Keep the generated directory private and load **that directory** as an unpacked
extension. The template in this repository cannot contact a real MCP server.

1. In the browser profile signed into X, open the extensions page, enable
   Developer mode, and load the generated directory.
2. Click **X MCP — Connect Account**, enter the X handle signed into that
   profile, and select its tier. The tier is an owner attestation, not a live
   entitlement check.
3. Approve access to X cookies, match the one-time code on your server's
   approval page, and enter your server's owner key there.
4. Wait for the server to verify the account identity and confirm import.
   Repeat for other accounts, one at a time.

The extension sends selected X session cookies only to the exact HTTPS origin
in the generated manifest and protocol file. It does so after a user click and
owner approval. It does not publish a post or change existing MCP client grants.
Store the server owner key and account sessions outside the repository.

The manifest's public key gives the extension a stable ID. The server uses that
ID for a narrow CORS allowlist; the one-time pairing grant and owner key remain
the actual authorization checks. Do not broaden host permissions or disable the
exact destination checks when adapting the extension.

See the [self-hosting guide](../README.md) for server setup and limitations.
