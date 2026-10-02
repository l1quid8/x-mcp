from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import BaseModel, ConfigDict, Field, model_validator

ORIGIN = os.environ.get("X_MCP_ORIGIN", "").rstrip("/")
_origin = urlsplit(ORIGIN)
if (_origin.scheme != "https" or not _origin.hostname or _origin.path or _origin.query
        or _origin.fragment or _origin.username or _origin.password):
    raise ValueError("Set X_MCP_ORIGIN to this installation's HTTPS origin without a path or credentials")
PREFIX = os.environ.get("X_MCP_PREFIX", "/x-mcp")
if not re.fullmatch(r"/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", PREFIX):
    raise ValueError("X_MCP_PREFIX must be a non-root URL path without a trailing slash")
RESOURCE = ORIGIN + PREFIX + "/mcp"
ISSUER = ORIGIN + PREFIX + "/oauth"
DEFAULT_SCOPES = ["x:read"]
X_ACCOUNT_SCOPES = ["publisher:status", "publisher:media", "publisher:publish",
                    "cleanup:read", "cleanup:plan", "cleanup:execute", "cleanup:protect"]
BUFFER_SCOPES = ["buffer:status", "buffer:publish"]
ACCOUNT_SCOPES = X_ACCOUNT_SCOPES + BUFFER_SCOPES
SCOPES = DEFAULT_SCOPES + ACCOUNT_SCOPES
TERMINAL = {"succeeded", "failed", "partial", "unknown"}


class Problem(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def identifier():
    return secrets.token_urlsafe(24)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FileInput(StrictModel):
    download_url: str
    file_id: str
    mime_type: str = ""
    file_name: str = ""


class Poll(StrictModel):
    choices: list[str] = Field(min_length=2, max_length=4)
    duration_minutes: int = Field(ge=5, le=10080)


class Attachment(StrictModel):
    media_id: str
    alt_text: str | None = Field(default=None, max_length=1000)


class Post(StrictModel):
    text: str = Field(default="", max_length=25000)
    attachments: list[Attachment] = Field(default_factory=list, max_length=4)
    reply_to: str | None = Field(default=None, pattern=r"^\d+$")
    quote_id: str | None = Field(default=None, pattern=r"^\d+$")
    poll: Poll | None = None
    long_post: bool = False

    @model_validator(mode="after")
    def check(self):
        if not self.text.strip() and not self.attachments:
            raise ValueError("A post needs text or media")
        if self.poll and self.attachments:
            raise ValueError("Polls cannot include uploaded media")
        if self.poll and any(not s.strip() or len(s) > 25 for s in self.poll.choices):
            raise ValueError("Poll choices must contain 1–25 characters")
        return self


class Article(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    markdown: str = Field(min_length=1, max_length=100000)
    cover_media_id: str | None = None
    media: list[Attachment] = Field(default_factory=list)


class Publication(StrictModel):
    kind: Literal["post", "thread", "article"]
    posts: list[Post] = Field(default_factory=list, max_length=100)
    article: Article | None = None

    @model_validator(mode="after")
    def check(self):
        if self.kind == "article":
            if not self.article or self.posts:
                raise ValueError("Article publication requires article and no posts")
        elif self.article or not self.posts or (self.kind == "post" and len(self.posts) != 1):
            raise ValueError("Provide one post or an ordered thread")
        if any(p.reply_to for p in self.posts[1:]):
            raise ValueError("Only the first thread entry may specify reply_to")
        return self


@dataclass(frozen=True)
class Principal:
    subject: str
    scopes: frozenset[str]
    accounts: frozenset[str]


principal_context = contextvars.ContextVar("publisher_principal", default=None)


def authorize(scope: str, account: str | None = None) -> Principal:
    p = principal_context.get()
    if p is None or scope not in p.scopes:
        raise Problem("insufficient_scope", "This connection lacks the required permission")
    if account is not None and account not in p.accounts:
        raise Problem("account_not_authorized", "Choose an account authorized for this connection")
    return p


class Store:
    def __init__(self, directory: Path, key: bytes):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory = directory
        self.media_directory = directory / "media"
        self.media_directory.mkdir(exist_ok=True, mode=0o700)
        self.cipher = Fernet(key)
        self.db = sqlite3.connect(directory / "publisher.sqlite3", timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY, username TEXT, session BLOB,
            capabilities TEXT, checked REAL, active INTEGER NOT NULL DEFAULT 1);
          CREATE TABLE IF NOT EXISTS tokens(hash TEXT PRIMARY KEY, label TEXT, scopes TEXT,
            accounts TEXT, expires REAL, active INTEGER DEFAULT 1);
          CREATE TABLE IF NOT EXISTS media(id TEXT PRIMARY KEY, account TEXT, name TEXT,
            size INTEGER, offset INTEGER DEFAULT 0, sha256 TEXT, metadata TEXT,
            status TEXT, expires REAL, importing INTEGER DEFAULT 0);
          CREATE TABLE IF NOT EXISTS drafts(id TEXT PRIMARY KEY, account TEXT, payload TEXT,
            hash TEXT, expires REAL);
          CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, account TEXT,
            idempotency TEXT, hash TEXT, payload TEXT, state TEXT, result TEXT,
            phase TEXT, created REAL, updated REAL, UNIQUE(account,idempotency));
          CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        ''')
        (directory / "publisher.sqlite3").chmod(0o600)

    def setting(self, key, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, canonical(value)))

    def buffer_key(self) -> str | None:
        """Read the encrypted Buffer personal key from private server state."""
        path = self.directory / "buffer-key.enc"
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(mode) or mode & 0o077:
            raise ValueError("Buffer key file must be regular and private (mode 600)")
        return self.cipher.decrypt(path.read_bytes()).decode("utf-8")

    def save_buffer_key(self, key: str) -> None:
        if not isinstance(key, str) or not key or len(key) > 4096 or "\r" in key or "\n" in key:
            raise ValueError("Invalid Buffer key")
        path = self.directory / "buffer-key.enc"
        fd, temp = tempfile.mkstemp(dir=self.directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as file:
                file.write(self.cipher.encrypt(key.encode("utf-8")))
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        # A new credential has no trusted channel grants until the provider is queried.
        self.save_buffer_channels([])

    def buffer_channels(self) -> list[dict]:
        if not self.buffer_key():
            return []
        return self.setting("buffer_channels", [])

    def save_buffer_channels(self, channels: list[dict]) -> None:
        """Persist channels verified against the configured Buffer credential."""
        if not isinstance(channels, list):
            raise ValueError("Expected a list of Buffer X channels")
        selected = []
        seen = set()
        for channel in channels:
            if not isinstance(channel, dict):
                raise ValueError("Invalid Buffer channel")
            account_id = channel.get("account_id")
            channel_id = channel.get("channel_id")
            if (not isinstance(channel_id, str) or not channel_id or len(channel_id) > 256
                    or not all(33 <= ord(c) <= 126 and c != ":" for c in channel_id)
                    or account_id != "buffer:" + channel_id or account_id in seen):
                raise ValueError("Invalid Buffer channel identifier")
            display_name = channel.get("display_name", "")
            handle = channel.get("handle", "")
            if (not isinstance(display_name, str) or len(display_name) > 256
                    or not isinstance(handle, str) or len(handle) > 256):
                raise ValueError("Invalid Buffer channel label")
            selected.append({"account_id": account_id, "channel_id": channel_id,
                             "display_name": display_name, "handle": handle})
            seen.add(account_id)
        self.set_setting("buffer_channels", selected)

    def buffer_channel(self, account_id: str) -> dict:
        if not isinstance(account_id, str) or not account_id.startswith("buffer:"):
            raise Problem("account_unavailable", "Choose a configured Buffer X channel")
        for channel in self.buffer_channels():
            if channel["account_id"] == account_id:
                return channel
        raise Problem("account_unavailable", "Choose a configured Buffer X channel")

    def validate_grant_accounts(self, scopes, accounts) -> None:
        """Never let a channel ID inherit an unrelated provider's permission."""
        scopes = set(scopes)
        for account_id in accounts:
            if account_id.startswith("buffer:"):
                if not scopes.intersection(BUFFER_SCOPES):
                    raise ValueError("Buffer channel requires a Buffer scope")
                self.buffer_channel(account_id)
            else:
                if not scopes.intersection(X_ACCOUNT_SCOPES):
                    raise ValueError("X account requires a publisher or cleanup scope")
                self.account(account_id)

    def account(self, account_id):
        row = self.db.execute("SELECT * FROM accounts WHERE id=? AND active=1", (account_id,)).fetchone()
        if not row:
            raise Problem("account_unavailable", "Import or reconnect this X account")
        return dict(row)

    def session(self, account_id):
        return json.loads(self.cipher.decrypt(self.account(account_id)["session"]))

    def save_account(self, account_id, username, cookies, capabilities):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO accounts VALUES (?,?,?,?,?,1)", (
                account_id, username, self.cipher.encrypt(canonical(cookies).encode()),
                canonical(capabilities), time.time()))

    def issue_token(self, label, scopes, accounts, days=90):
        if not scopes or set(scopes) - set(SCOPES):
            raise ValueError("A known scope is required")
        if not set(scopes).intersection(ACCOUNT_SCOPES):
            accounts = []
        self.validate_grant_accounts(scopes, accounts)
        token = secrets.token_urlsafe(48)
        with self.db:
            self.db.execute("INSERT INTO tokens VALUES (?,?,?,?,?,1)", (
                hashlib.sha256(token.encode()).hexdigest(), label, canonical(scopes),
                canonical(accounts), time.time() + days * 86400))
        return token

    def token_principal(self, token):
        row = self.db.execute("SELECT * FROM tokens WHERE hash=? AND active=1 AND expires>?", (
            hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        if row:
            return Principal("owner", frozenset(json.loads(row["scopes"])), frozenset(json.loads(row["accounts"])))

    def media(self, media_id, account):
        row = self.db.execute("SELECT * FROM media WHERE id=? AND account=?", (media_id, account)).fetchone()
        if not row or row["expires"] <= time.time():
            raise Problem("media_unavailable", "Attachment is missing, expired, or belongs to another account")
        return dict(row)

    def operation(self, operation_id):
        row = self.db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
        if not row:
            raise Problem("operation_not_found", "Unknown operation")
        return dict(row)

    def update_operation(self, operation_id, state, result, phase=None):
        with self.db:
            self.db.execute("UPDATE operations SET state=?,result=?,phase=?,updated=? WHERE id=?", (
                state, canonical(result), phase, time.time(), operation_id))

    def recover(self):
        # Never replay a request after a process crash: the remote outcome may be unknown.
        with self.db:
            self.db.execute("UPDATE operations SET state='unknown',phase='restart_interrupted',updated=? WHERE state NOT IN ('succeeded','failed','partial','unknown')", (time.time(),))
            self.db.execute("UPDATE media SET importing=0 WHERE importing=1")


def runtime_store():
    credentials = Path(os.environ.get("CREDENTIALS_DIRECTORY", "/etc/x-mcp"))
    return Store(Path(os.environ.get("X_MCP_STATE_DIR", "/var/lib/x-mcp")),
                 (credentials / "encryption-key").read_bytes().strip())
