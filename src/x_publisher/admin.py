"""Local administration. Secrets go only to private files or an explicit helper pipe."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import pwd
import secrets
import tempfile

from cryptography.fernet import Fernet

from .backend import XBackend, capability_defaults
from .core import DEFAULT_SCOPES, RESOURCE, SCOPES, canonical, runtime_store


def secret_file(path, value, user=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        if user:
            entry = pwd.getpwnam(user)
            os.fchown(fd, entry.pw_uid, entry.pw_gid)
        with os.fdopen(fd, "w") as f:
            f.write(value)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


async def import_session(store, args):
    data = json.loads(Path(args.file).read_text())
    if isinstance(data, dict) and "cookies" in data:
        data = data["cookies"]
    if isinstance(data, list):
        data = {c["name"]: c["value"] for c in data if c.get("domain", "").lstrip(".") in {"x.com", "twitter.com"}}
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise ValueError("Expected a cookie dictionary or browser cookie export")
    cookies = {k: v for k, v in data.items() if k in {"auth_token", "ct0", "twid", "kdt", "att", "lang"}}
    if not cookies.get("auth_token") or not cookies.get("ct0"):
        raise ValueError("The export must include auth_token and ct0")
    backend = XBackend(cookies)
    try:
        identity = await backend.identity()
        if identity["username"].lower() != args.expected_user.lstrip("@").lower():
            raise ValueError("Session identity does not match the expected account")
        store.save_account(identity["id"], identity["username"], backend.client.get_cookies(), capability_defaults(args.tier))
        print(json.dumps({"imported": identity, "tier_source": "owner_attested", "publishing_verified": False}))
    finally:
        await backend.close()


def main():
    if os.geteuid() != 0:
        raise SystemExit("Run this administrative command with sudo")
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    imp = sub.add_parser("import-session")
    imp.add_argument("--file", required=True)
    imp.add_argument("--expected-user", required=True)
    imp.add_argument("--tier", choices=["unknown", "free", "basic", "premium", "premium_plus"], default="unknown")
    pair = sub.add_parser("pair-session", help="Create a one-time grant for the local browser helper")
    pair.add_argument("--expected-user", required=True)
    pair.add_argument("--tier", choices=["unknown", "free", "basic", "premium", "premium_plus"], default="unknown")
    pair.add_argument("--stdout", action="store_true", help="Send credential directly to a helper pipe, never a terminal")
    pair.add_argument("--output")
    pair.add_argument("--owner", default=os.environ.get("X_MCP_OWNER_USER"))
    sub.add_parser("revoke-pairings")
    sub.add_parser("accounts")
    dis = sub.add_parser("disconnect")
    dis.add_argument("account_id")
    token = sub.add_parser("issue-client")
    token.add_argument("--label", default="desktop")
    token.add_argument("--accounts", nargs="*", default=[])
    token.add_argument("--output", default=str(Path.home() / "x-mcp-connection.json"))
    token.add_argument("--owner", default=os.environ.get("X_MCP_OWNER_USER"))
    token.add_argument("--scopes", nargs="+", choices=SCOPES, default=DEFAULT_SCOPES)
    config_x = sub.add_parser("configure-x-oauth", help="Read official app credentials from a private JSON file")
    config_x.add_argument("--file", required=True)
    connect_x = sub.add_parser("connect-x-oauth", help="Create an official X PKCE authorization link for one existing account")
    connect_x.add_argument("--account", required=True)
    connect_x.add_argument("--with-dms", action="store_true")
    connect_x.add_argument("--output", required=True)
    connect_x.add_argument("--owner", default=os.environ.get("X_MCP_OWNER_USER"))
    approve = sub.add_parser("approve-deletion-plan", help="Approve an exact reviewed digest; never executes deletion")
    approve.add_argument("--account", required=True)
    approve.add_argument("--plan", required=True)
    approve.add_argument("--digest", required=True)
    approve.add_argument("--max-actions", type=int, default=1)
    approve.add_argument("--minutes", type=int, default=10)
    revoke_plan = sub.add_parser("revoke-deletion-plan")
    revoke_plan.add_argument("--plan", required=True)
    revoke = sub.add_parser("revoke-client")
    revoke.add_argument("label")
    callbacks = sub.add_parser("allow-callback")
    callbacks.add_argument("url")
    sub.add_parser("revoke-oauth")
    args = parser.parse_args()
    secrets_dir = Path(os.environ.get("CREDENTIALS_DIRECTORY", "/etc/x-mcp"))
    secrets_dir.mkdir(mode=0o700, exist_ok=True)
    if args.command == "init":
        for name, value in [("encryption-key", Fernet.generate_key().decode()), ("owner-key", secrets.token_urlsafe(48))]:
            if not (secrets_dir / name).exists():
                secret_file(secrets_dir / name, value + "\n")
    store = runtime_store()
    try:
        if args.command == "import-session":
            asyncio.run(import_session(store, args))
        elif args.command == "pair-session":
            import sys
            from .pairing import issue_pairing
            if bool(args.stdout) == bool(args.output) or (args.stdout and sys.stdout.isatty()):
                raise ValueError("Use a helper pipe or a private --output file")
            grant = issue_pairing(store, args.expected_user, args.tier)
            if args.stdout:
                print(json.dumps(grant))
            else:
                secret_file(args.output, json.dumps(grant), args.owner)
                print("Private one-time pairing file saved to " + args.output)
        elif args.command == "revoke-pairings":
            from .pairing import schema
            schema(store)
            with store.db:
                store.db.execute("DELETE FROM pairings")
            import sqlite3
            path = store.directory / "oauth.sqlite3"
            if path.exists():
                with sqlite3.connect(path) as db:
                    db.execute("DELETE FROM records WHERE kind LIKE 'pair-%'")
            print("All pending session pairings and browser approvals revoked")
        elif args.command == "accounts":
            print(json.dumps([dict(r) for r in store.db.execute("SELECT id,username,capabilities,checked,active FROM accounts")], indent=2))
        elif args.command == "disconnect":
            with store.db:
                store.db.execute("UPDATE accounts SET active=0,session=NULL WHERE id=?", (args.account_id,))
            print("Account disconnected; existing client grants no longer make it usable")
        elif args.command == "issue-client":
            with store.db:
                store.db.execute("UPDATE tokens SET active=0 WHERE label=?", (args.label,))
            token = store.issue_token(args.label, args.scopes, args.accounts)
            secret_file(args.output, json.dumps({"url": RESOURCE, "token": token, "accounts": args.accounts}, indent=2) + "\n", args.owner)
            print("Saved private client configuration to " + args.output)
        elif args.command in {"configure-x-oauth", "connect-x-oauth", "approve-deletion-plan", "revoke-deletion-plan"}:
            from .cleanup import Cleanup
            import time
            cleanup = Cleanup(store)
            try:
                if args.command == "configure-x-oauth":
                    path = Path(args.file)
                    if path.stat().st_mode & 0o077:
                        raise ValueError("OAuth credential input must be private (mode 600)")
                    config = json.loads(path.read_text())
                    if set(config) - {"client_id", "client_secret"}:
                        raise ValueError("Expected only official OAuth app credentials")
                    cleanup.oauth.save_app(config["client_id"], config.get("client_secret"))
                    print("Official X OAuth app configuration stored encrypted")
                elif args.command == "connect-x-oauth":
                    url = cleanup.oauth.begin(args.account, args.with_dms)
                    secret_file(args.output, url+"\n", args.owner)
                    print("Official X authorization link saved to private file " + args.output)
                elif args.command == "approve-deletion-plan":
                    plan = cleanup.repo.plan(args.account, args.plan)
                    total = json.loads(plan["payload"])["total_actions"]
                    if plan["digest"] != args.digest or not 1 <= args.max_actions <= total * 5 or not 1 <= args.minutes <= 60:
                        raise ValueError("Digest, action budget or expiry does not match the reviewed plan")
                    # Approval budgets are cumulative across all runs and request keys.
                    with cleanup.repo.db:
                        cleanup.repo.db.execute("INSERT OR REPLACE INTO approvals VALUES (?,?,?,?)", (
                            args.plan, args.digest, time.time()+args.minutes*60, args.max_actions))
                    print("Exact plan approved with a cumulative action budget; no deletion executed")
                else:
                    with cleanup.repo.db:
                        cleanup.repo.db.execute("DELETE FROM approvals WHERE plan=?", (args.plan,))
                    print("Plan execution approval revoked")
            finally:
                cleanup.repo.db.close()
        elif args.command == "revoke-client":
            with store.db:
                store.db.execute("UPDATE tokens SET active=0 WHERE label=?", (args.label,))
            print("Matching private client tokens revoked")
        elif args.command == "allow-callback":
            from .auth import callback_key
            callback = callback_key(args.url)
            if callback is None:
                raise ValueError("Use the exact ChatGPT HTTPS callback or Codex 127.0.0.1 callback path")
            allowed = store.setting("callbacks", [])
            store.set_setting("callbacks", sorted(set(allowed + [callback])))
            print("Callback path approved; reconnect the MCP client")
        elif args.command == "revoke-oauth":
            import sqlite3
            path = store.directory / "oauth.sqlite3"
            if path.exists():
                with sqlite3.connect(path) as db:
                    db.execute("DELETE FROM records")
            print("OAuth clients and grants revoked")
        elif args.command == "init":
            print("Independent publisher keys and state initialized")
    except Exception as exc:
        # Third-party exceptions may embed upstream requests or cookies.
        from .core import Problem
        if isinstance(exc, Problem):
            raise SystemExit(str(exc)) from None
        raise SystemExit(f"Operation failed ({type(exc).__name__}); no credential details printed") from None
    finally:
        store.db.close()
        user = pwd.getpwnam(os.environ.get("X_MCP_SERVICE_USER", "xmcp"))
        for path in [store.directory, store.media_directory, *store.directory.glob("*.sqlite3*")]:
            os.chown(path, user.pw_uid, user.pw_gid)
            if path.is_file():
                path.chmod(0o600)


if __name__ == "__main__":
    main()
