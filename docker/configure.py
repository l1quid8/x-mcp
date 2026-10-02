#!/usr/bin/env python3
"""Create the private Compose configuration for one self-hosted origin."""
import os
from pathlib import Path
import re
import secrets
import sys
from urllib.parse import urlsplit


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python3 docker/configure.py https://your-mcp-host")
    origin = sys.argv[1].rstrip("/")
    parsed = urlsplit(origin)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.path or parsed.query or parsed.fragment
            or parsed.username or parsed.password or parsed.hostname.endswith(".example.com")
            or not re.fullmatch(r"https://[A-Za-z0-9.\-\[\]:]+", origin)):
        raise SystemExit("Enter your own HTTPS origin without a path or credentials")
    try:
        if parsed.port == 0:
            raise ValueError()
    except ValueError:
        raise SystemExit("Invalid HTTPS port") from None
    target = Path(".env")
    os.umask(0o077)
    try:
        with target.open("x", encoding="utf-8") as f:
            f.write("X_MCP_ORIGIN=" + origin + "\n")
            f.write("X_MCP_PREFIX=/x-mcp\n")
            f.write("X_MCP_BROWSER_WORKER_TOKEN=" + secrets.token_hex(32) + "\n")
    except FileExistsError:
        raise SystemExit(".env already exists; keep its existing private keys and origin") from None
    print("Private .env created for " + origin)


if __name__ == "__main__":
    main()
