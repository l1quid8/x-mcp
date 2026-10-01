#!/usr/bin/env python3
"""Connect an owned X account without exporting cookies to a file."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import webbrowser
from urllib.parse import urlsplit, parse_qs
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

ORIGIN = os.environ.get("X_MCP_ORIGIN", "").rstrip("/")
_origin = urlsplit(ORIGIN)
try:
    _origin_port = _origin.port
except ValueError as exc:
    raise ValueError("X_MCP_ORIGIN has an invalid port") from exc
if (_origin.scheme != "https" or not _origin.hostname or _origin.path or _origin.query
        or _origin.fragment or _origin.username or _origin.password or _origin_port == 0):
    raise ValueError("Set X_MCP_ORIGIN to your own HTTPS server origin")
HOST = _origin.hostname
NETLOC = _origin.netloc
PREFIX = os.environ.get("X_MCP_PREFIX", "/x-mcp")
if not re.fullmatch(r"/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", PREFIX):
    raise ValueError("X_MCP_PREFIX must be a non-root URL path")
ENDPOINT = ORIGIN + PREFIX + "/session-import"
ADMIN = os.environ.get("X_MCP_ADMIN_PATH", "/opt/x-mcp/.venv/bin/x-mcp-admin")
SSH_PORT = int(os.environ.get("X_MCP_SSH_PORT", "22"))
if not 1 <= SSH_PORT <= 65535:
    raise ValueError("X_MCP_SSH_PORT must be between 1 and 65535")
COOKIE_NAMES = {"auth_token", "ct0", "twid", "kdt", "att", "lang"}


class ConnectionFailure(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ConnectionFailure("The server redirected the import. No credentials were forwarded.")


def ssh_admin(user, arguments):
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
        raise ConnectionFailure("Invalid server SSH username")
    command = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "ConnectTimeout=15", "-p", str(SSH_PORT), user + "@" + HOST,
               shlex.join(["sudo", "-n", ADMIN, *arguments])]
    try:
        result = subprocess.run(command, capture_output=True, timeout=40, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ConnectionFailure("Could not reach the server through SSH. Check your existing administrator SSH connection.") from None
    if result.returncode:
        raise ConnectionFailure("SSH administrator access failed. Check your SSH key, trusted host entry, and passwordless sudo; use --ssh-user if your server login differs.")
    return result.stdout


def pairing(user, account, tier):
    raw = ssh_admin(user, ["pair-session", "--expected-user", account, "--tier", tier, "--stdout"])
    try:
        data = json.loads(raw)
        if (data["endpoint"] != ENDPOINT or data["expected_user"] != account
            or not re.fullmatch(r"[A-Za-z0-9_-]{64}", data["pairing_token"])):
            raise ValueError()
        return data["pairing_token"]
    except (ValueError, KeyError, TypeError):
        raise ConnectionFailure("The server returned an unexpected pairing response; no cookies were sent.") from None



def browser_pairing(account, tier):
    base = ORIGIN + PREFIX + "/oauth/pair"
    opener = build_opener(ProxyHandler({}), NoRedirect())
    def request(path, data=None, token=None):
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            headers["Authorization"] = "Device " + token
        try:
            with opener.open(Request(base+path, method="POST", headers=headers,
                    data=json.dumps(data or {}).encode()), timeout=30) as response:
                return json.loads(response.read(16384))
        except Exception:
            raise ConnectionFailure("HTTPS pairing failed or expired. Run the helper again; no SSH connection is needed.") from None
    started = request("/start", {"account": account, "tier": tier})
    try:
        token, code, url = started["device_code"], started["user_code"], started["verification_uri"]
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        if (parts.scheme != "https" or parts.netloc != NETLOC or parts.path != PREFIX + "/oauth/pair/approve"
            or parts.fragment or set(query) != {"request"} or len(query["request"]) != 1
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", query["request"][0])
            or not re.fullmatch(r"[A-Za-z0-9_-]{64}", token)
            or not re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}", code)):
            raise ValueError()
    except Exception:
        raise ConnectionFailure("Unexpected pairing page; no session was transferred.") from None
    print("Your pairing code: " + code)
    print("Opening your X MCP HTTPS approval page. Check the code and enter your X MCP owner key there.")
    if not webbrowser.open(url):
        print("Open this pairing page in your browser: " + url)
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        time.sleep(2)
        result = request("/poll", token=token)
        if result.get("state") == "pending":
            continue
        if (result.get("endpoint") != ENDPOINT or result.get("expected_user") != account
            or not re.fullmatch(r"[A-Za-z0-9_-]{64}", result.get("pairing_token", ""))):
            raise ConnectionFailure("Unexpected pairing grant; no session was transferred.")
        return result["pairing_token"]
    raise ConnectionFailure("Pairing expired. Run the helper again when ready.")


def selected_cookies(export):
    cookies = {c["name"]: c["value"] for c in export
               if c.get("domain", "").lstrip(".") == "x.com" and c.get("name") in COOKIE_NAMES}
    if not cookies.get("auth_token") or not cookies.get("ct0"):
        raise ConnectionFailure("X login is not complete. Run the helper again and finish signing into the selected account.")
    return cookies


def transfer(cookies, token):
    # No proxy discovery, redirects, URL credentials, or cookie-bearing command arguments.
    opener = build_opener(ProxyHandler({}), NoRedirect())
    request = Request(ENDPOINT, method="POST", data=json.dumps({"cookies": cookies}).encode(),
        headers={"Authorization": "Pairing " + token, "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with opener.open(request, timeout=110) as response:
            result = json.loads(response.read(16385))
    except HTTPError as exc:
        try:
            code = json.loads(exc.read(16384)).get("error")
        except Exception:
            code = None
        messages = {
            "account_mismatch": "You signed into a different account. Nothing was saved; reconnect and select the intended account.",
            "invalid_pairing": "The pairing expired or was used. Run the helper again to create a fresh pairing.",
            "account_busy": "The account is currently publishing. Reconnect after that operation finishes.",
            "session_or_account_restricted": "X rejected the session. Resolve any challenge in X before reconnecting.",
            "rate_limited": "X rate-limited session verification. Wait before reconnecting.",
            "import_busy": "Another account is connecting. Wait for it to finish, then run the helper again.",
        }
        raise ConnectionFailure(messages.get(code, "The server could not verify this session. No automatic retry was attempted.")) from None
    except ConnectionFailure:
        raise
    except Exception:
        raise ConnectionFailure("The import response was interrupted. Check the server account list before reconnecting; the session may already be saved.") from None
    if not result.get("session_verified") or not str(result.get("account_id", "")).isdigit() or not re.fullmatch(r"[A-Za-z0-9_]{1,15}", result.get("username", "")):
        raise ConnectionFailure("Unexpected import response. Check the server account list before reconnecting.")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", help="Owned X handle to connect")
    parser.add_argument("--tier", choices=["unknown", "free", "basic", "premium", "premium_plus"])
    parser.add_argument("--pairing-method", choices=["https", "ssh"], default="https")
    parser.add_argument("--ssh-user", default=os.environ.get("X_MCP_SSH_USER"), help="Only used with --pairing-method ssh")
    parser.add_argument("--install-browser", action="store_true", help="Install the pinned Playwright Chromium browser if needed")
    args = parser.parse_args()
    os.umask(0o077)
    # Prevent inherited Playwright debugging from logging browser traffic.
    for key in ("DEBUG", "PWDEBUG", "DEBUG_FILE"):
        os.environ.pop(key, None)
    if not args.account:
        args.account = input("X handle to connect: ").strip()
    account = args.account.lstrip("@").lower()
    if not re.fullmatch(r"[a-z0-9_]{1,15}", account):
        raise ConnectionFailure("Enter a valid X handle")
    tier = args.tier or "unknown"
    if args.pairing_method == "ssh":
        if not args.ssh_user:
            raise ConnectionFailure("Set X_MCP_SSH_USER or pass --ssh-user for SSH pairing")
        print("Checking your existing administrator SSH connection...")
        ssh_admin(args.ssh_user, ["--help"])
    if args.install_browser:
        result = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)
        if result.returncode:
            raise ConnectionFailure("Browser installation failed. Check your connection and supported macOS version.")
    from playwright.sync_api import sync_playwright
    print(f"Sign into @{account} in the browser window. Complete any X verification yourself.")
    print("No posts will be sent. This connects the selected account to your private X MCP server.")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context()
        try:
            page = context.new_page()
            page.goto("https://x.com/i/flow/login", wait_until="domcontentloaded", timeout=60000)
            input("When you can see your X home feed, return here and press Enter to connect: ")
            cookies = selected_cookies(context.cookies("https://x.com"))
            token = browser_pairing(account, tier) if args.pairing_method == "https" else pairing(args.ssh_user, account, tier)
            result = transfer(cookies, token)
            cookies.clear()
            token = None
            print(f"Connected @{result['username']} (account {result['account_id']}). Session stored encrypted on your server.")
            print("No cookie export was created. Client account grants are unchanged; publishing was not tested.")
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("Connection canceled.", file=sys.stderr)
        sys.exit(1)
    except ConnectionFailure as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    except Exception:
        print("Connection failed. No diagnostic containing session credentials was printed. Resolve the browser/login issue and try again.", file=sys.stderr)
        sys.exit(1)
