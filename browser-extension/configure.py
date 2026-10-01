#!/usr/bin/env python3
"""Generate an X MCP connector pinned to one self-hosted HTTPS origin."""

import argparse
from pathlib import Path
import re
from urllib.parse import urlsplit


TEMPLATE_ORIGIN = "https://mcp.example.invalid"
ASSETS = ("manifest.json", "protocol.mjs", "background.js", "connect.html", "connect.js", "style.css")


def valid_origin(value: str) -> str:
    origin = value.rstrip("/")
    parsed = urlsplit(origin)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Use a valid HTTPS port") from exc
    if (parsed.scheme != "https" or not parsed.hostname or parsed.path or parsed.query
            or parsed.fragment or parsed.username or parsed.password or port == 0
            or origin == TEMPLATE_ORIGIN or any(c.isspace() for c in origin)):
        raise ValueError("Provide your own HTTPS origin without a path or credentials")
    return origin


def configure(origin: str, output: Path, prefix: str = "/x-mcp") -> Path:
    origin = valid_origin(origin)
    if not re.fullmatch(r"/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", prefix):
        raise ValueError("Prefix must be a non-root URL path without a trailing slash")
    source = Path(__file__).resolve().parent
    output = output.expanduser().resolve()
    if output == source or source in output.parents or output.exists():
        raise ValueError("Choose a new output directory outside the source extension")
    output.mkdir(parents=True)
    for name in ASSETS:
        body = (source / name).read_text(encoding="utf-8")
        if name in {"manifest.json", "protocol.mjs"}:
            body = body.replace(TEMPLATE_ORIGIN, origin)
        if name == "protocol.mjs":
            body = body.replace("/x-mcp", prefix)
        (output / name).write_text(body, encoding="utf-8")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True, help="Your server's HTTPS origin, such as https://mcp.example.com")
    parser.add_argument("--output", required=True, type=Path, help="New directory for the configured extension")
    parser.add_argument("--prefix", default="/x-mcp", help="Match X_MCP_PREFIX on your server")
    args = parser.parse_args()
    try:
        destination = configure(args.origin, args.output, args.prefix)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(destination)


if __name__ == "__main__":
    main()
