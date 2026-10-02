#!/bin/sh
set -eu
umask 077
mkdir -p "$CREDENTIALS_DIRECTORY" "$X_MCP_STATE_DIR" "$X_MCP_READER_STATE_DIR"
if [ ! -f "$CREDENTIALS_DIRECTORY/encryption-key" ]; then
    /opt/x-mcp/.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())' > "$CREDENTIALS_DIRECTORY/encryption-key"
fi
if [ ! -f "$CREDENTIALS_DIRECTORY/owner-key" ]; then
    /opt/x-mcp/.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(48))' > "$CREDENTIALS_DIRECTORY/owner-key"
fi
chown -R xmcp:xmcp /var/lib/x-mcp
exec gosu xmcp /opt/x-mcp/.venv/bin/x-mcp
