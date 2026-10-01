#!/usr/bin/env python3
"""Upload one explicitly selected local file. Uses only Python's standard library."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import stat
import sys
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
from urllib.error import HTTPError, URLError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--account", required=True, help="Numeric X account ID from publishing_status")
    parser.add_argument("--config", type=Path, default=Path.home()/".config/x-mcp/connection.json")
    parser.add_argument("--resume", help="Previously returned media ID")
    args = parser.parse_args()
    if args.config.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        parser.error("Restrict the credential file with chmod 600 before using it")
    config = json.loads(args.config.read_text())
    endpoint, token = config["url"], config["token"]
    parts = urlsplit(endpoint)
    root_path = parts.path.removesuffix("/mcp")
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or not parts.path.endswith("/mcp")
            or not re.fullmatch(r"/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", root_path)):
        parser.error("X MCP endpoint must be an HTTPS MCP URL without credentials, query, or fragment")
    root = "https://" + parts.netloc + root_path
    version = None
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise ValueError("X MCP redirects are not followed with credentials")
    opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(url, method="POST", data=None, headers=None):
        destination = urlsplit(url)
        if (destination.scheme != "https" or destination.netloc != parts.netloc
                or destination.username or destination.password or destination.query or destination.fragment
                or (destination.path != parts.path and not destination.path.startswith(root_path + "/uploads/"))):
            raise ValueError("Refusing to send credentials outside the configured X MCP endpoint")
        hs = {"Authorization":"Bearer "+token, "Accept":"application/json, text/event-stream"}
        if version:
            hs["MCP-Protocol-Version"] = version
        hs.update(headers or {})
        body = data
        if isinstance(data, dict):
            body = json.dumps(data).encode()
            hs["Content-Type"] = "application/json"
        with opener.open(Request(url, data=body, headers=hs, method=method), timeout=180) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}

    counter = 0
    def rpc(method, params):
        nonlocal counter
        counter += 1
        response = request(endpoint, data={"jsonrpc":"2.0","id":counter,"method":method,"params":params})
        if "error" in response:
            raise ValueError("MCP request failed; check your client credential and account access")
        return response["result"]

    def tool(name, arguments):
        response = rpc("tools/call", {"name":name,"arguments":arguments})
        if response.get("isError"):
            raise ValueError("X MCP refused the operation; verify account access, size, and quota")
        return response["structuredContent"]

    initialized = rpc("initialize", {"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"x-mcp-upload","version":"0.1.0"}})
    version = initialized["protocolVersion"]
    request(endpoint,data={"jsonrpc":"2.0","method":"notifications/initialized"})
    path = args.file.resolve(strict=True)
    if not path.is_file():
        parser.error("Select a regular file")
    size = path.stat().st_size
    if args.resume:
        if any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for c in args.resume):
            parser.error("Invalid media ID")
        mid = args.resume
        url = root + "/uploads/" + mid
        progress = request(url, "GET")
        if progress["size"] != size:
            parser.error("Local file size differs from the reserved upload")
        offset = progress["offset"]
    else:
        with path.open("rb") as f:
            digest = hashlib.file_digest(f,"sha256").hexdigest()
        upload = tool("begin_media_upload", {"account_id":args.account,"name":path.name,"size":size,"sha256":digest})
        mid, url, offset = upload["media_id"], upload["upload_url"], 0
    print("Upload ID: " + mid + " (use --resume " + mid + " after interruption)", file=sys.stderr)
    with path.open("rb") as f:
        f.seek(offset)
        while chunk := f.read(4*1024*1024):
            response = request(url,"PUT",chunk,{"Upload-Offset":str(offset),"Content-Type":"application/octet-stream"})
            offset = response["offset"]
    response = request(url,"POST",b"")
    print(json.dumps(response))


if __name__ == "__main__":
    try:
        main()
    except (HTTPError, URLError, OSError, ValueError, KeyError) as exc:
        print("Upload failed ("+type(exc).__name__+"). Check the connection, permissions, and resume offset. No credential details printed.",file=sys.stderr)
        raise SystemExit(1)
