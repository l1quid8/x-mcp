"""Deterministic cleanup planning and durable bounded execution."""
import asyncio
from collections import Counter
from contextvars import ContextVar
from functools import wraps
from datetime import datetime, timezone
import hashlib
import json
import os
import sqlite3
import time

from .cleanup_models import Candidate, ProposedAction, ProtectionPolicy
from .core import Problem, canonical, identifier
from .x_api import XAPIError, XOAuth
from .session_cleanup import SessionCleanup

_backends = ContextVar('cleanup_backends', default=None)


def backend_lifetime(fn):
    @wraps(fn)
    async def wrapped(*args, **kwargs):
        backends = []
        token = _backends.set(backends)
        try:
            return await fn(*args, **kwargs)
        finally:
            _backends.reset(token)
            for backend in backends:
                if hasattr(backend, 'close'):
                    await backend.close()
    return wrapped

ACTION_FOR = {"POST": "DELETE_POST", "REPLY": "DELETE_POST", "QUOTE": "DELETE_POST",
              "REPOST": "UNDO_REPOST", "DM": "DELETE_DM"}
LOCAL_OBSERVATION_SOURCES = {'owner_browser_observation', 'public_x_embed'}


class CleanupRepository:
    def __init__(self, store):
        self.store = store
        path = store.directory / "cleanup.sqlite3"
        self.db = sqlite3.connect(path, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS credentials(account TEXT PRIMARY KEY,value BLOB NOT NULL);
          CREATE TABLE IF NOT EXISTS auth_requests(hash TEXT PRIMARY KEY,payload BLOB,expires REAL);
          CREATE TABLE IF NOT EXISTS candidates(id TEXT PRIMARY KEY,account TEXT,payload TEXT,created REAL);
          CREATE INDEX IF NOT EXISTS candidates_account ON candidates(account,created);
          CREATE TABLE IF NOT EXISTS scan_cursors(id TEXT PRIMARY KEY,account TEXT,kind TEXT,token TEXT,expires REAL);
          CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY,account TEXT,payload TEXT,created REAL);
          CREATE TABLE IF NOT EXISTS protections(account TEXT PRIMARY KEY,payload TEXT);
          CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY,account TEXT,payload TEXT,digest TEXT,created REAL);
          CREATE TABLE IF NOT EXISTS actions(plan TEXT REFERENCES plans(id),seq INTEGER,payload TEXT,
            PRIMARY KEY(plan,seq));
          CREATE TABLE IF NOT EXISTS plan_seals(plan TEXT PRIMARY KEY REFERENCES plans(id));
          CREATE TABLE IF NOT EXISTS progress(plan TEXT,seq INTEGER,state TEXT,attempts INTEGER DEFAULT 0,
            retry_at REAL DEFAULT 0,receipt TEXT DEFAULT '{}',PRIMARY KEY(plan,seq),
            FOREIGN KEY(plan,seq) REFERENCES actions(plan,seq));
          CREATE INDEX IF NOT EXISTS progress_state ON progress(plan,state,seq);
          CREATE TABLE IF NOT EXISTS runs(plan TEXT PRIMARY KEY,owner TEXT,lease REAL);
          CREATE TABLE IF NOT EXISTS execution_keys(account TEXT,key TEXT,plan TEXT,mode TEXT,
            PRIMARY KEY(account,key));
          CREATE TABLE IF NOT EXISTS target_claims(account TEXT,action TEXT,target TEXT,plan TEXT,seq INTEGER,
            PRIMARY KEY(account,action,target));
          CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT,account TEXT,plan TEXT,
            seq INTEGER,target TEXT,content_type TEXT,action TEXT,decision TEXT,timestamp REAL,result TEXT,receipt TEXT);
          CREATE INDEX IF NOT EXISTS audit_account ON audit(account,id);
          CREATE TABLE IF NOT EXISTS approvals(plan TEXT PRIMARY KEY,digest TEXT,expires REAL,max_actions INTEGER);
          CREATE TABLE IF NOT EXISTS browser_observations(candidate TEXT PRIMARY KEY,account TEXT,digest TEXT,expires REAL,evidence TEXT);
          CREATE TRIGGER IF NOT EXISTS frozen_plan_update BEFORE UPDATE ON plans BEGIN SELECT RAISE(ABORT,'immutable plan'); END;
          CREATE TRIGGER IF NOT EXISTS frozen_plan_delete BEFORE DELETE ON plans BEGIN SELECT RAISE(ABORT,'immutable plan'); END;
          CREATE TRIGGER IF NOT EXISTS frozen_action_update BEFORE UPDATE ON actions BEGIN SELECT RAISE(ABORT,'immutable action'); END;
          CREATE TRIGGER IF NOT EXISTS frozen_action_delete BEFORE DELETE ON actions BEGIN SELECT RAISE(ABORT,'immutable action'); END;
          CREATE TRIGGER IF NOT EXISTS frozen_action_insert BEFORE INSERT ON actions WHEN EXISTS(SELECT 1 FROM plan_seals WHERE plan=NEW.plan) BEGIN SELECT RAISE(ABORT,'sealed plan'); END;
          CREATE TRIGGER IF NOT EXISTS frozen_candidate_update BEFORE UPDATE ON candidates BEGIN SELECT RAISE(ABORT,'immutable snapshot'); END;
          CREATE TRIGGER IF NOT EXISTS frozen_candidate_delete BEFORE DELETE ON candidates BEGIN SELECT RAISE(ABORT,'immutable snapshot'); END;
          CREATE TRIGGER IF NOT EXISTS frozen_batch_update BEFORE UPDATE ON batches BEGIN SELECT RAISE(ABORT,'immutable batch'); END;
          CREATE TRIGGER IF NOT EXISTS frozen_batch_delete BEFORE DELETE ON batches BEGIN SELECT RAISE(ABORT,'immutable batch'); END;
          CREATE TRIGGER IF NOT EXISTS audit_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT,'append only audit'); END;
          CREATE TRIGGER IF NOT EXISTS audit_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT,'append only audit'); END;
        ''')
        path.chmod(0o600)

    def save_credential(self, account, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO credentials VALUES (?,?)", (
                account, self.store.cipher.encrypt(canonical(value).encode())))

    def policy(self, account):
        row = self.db.execute("SELECT payload FROM protections WHERE account=?", (account,)).fetchone()
        return ProtectionPolicy.model_validate_json(row[0]) if row else ProtectionPolicy()

    def set_policy(self, account, policy):
        self.store.account(account)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO protections VALUES (?,?)", (account, canonical(policy.model_dump())))
        return policy.model_dump()

    def candidate(self, account, cid):
        row = self.db.execute("SELECT payload FROM candidates WHERE id=? AND account=?", (cid, account)).fetchone()
        if not row:
            raise Problem("candidate_unavailable", "Candidate was not retrieved by this account")
        return json.loads(row[0])

    def verify_browser_observation(self, account, candidate):
        row = self.db.execute('SELECT * FROM browser_observations WHERE candidate=? AND account=?',
            (candidate['candidate_id'], account)).fetchone()
        digest = hashlib.sha256(canonical(candidate).encode()).hexdigest()
        if (not row or row['digest'] != digest or row['expires'] <= time.time()
                or candidate['source'] not in LOCAL_OBSERVATION_SOURCES
                or candidate['author_id'] != account or candidate['content_type'] != 'POST'):
            raise Problem('browser_observation_unverified', 'Local administrator must review this exact browser observation before use')

    def plan(self, account, pid):
        row = self.db.execute("SELECT * FROM plans WHERE id=? AND account=?", (pid, account)).fetchone()
        if not row:
            raise Problem("plan_unavailable", "Unknown plan or different account")
        return dict(row)


def normalize(account, raw, includes=None, dm=False, source="x_api_v2"):
    includes = includes or {}
    refs = {r["type"]: r["id"] for r in raw.get("referenced_posts", raw.get("referenced_tweets", []))}
    kind = "DM" if dm else ("REPOST" if "retweeted" in refs else "REPLY" if "replied_to" in refs else "QUOTE" if "quoted" in refs else "POST")
    author_id = raw.get("sender_id" if dm else "author_id")
    users = {u["id"]: u for u in includes.get("users", [])}
    media = {m["media_key"]: m for m in includes.get("media", [])}
    related_ids = set(refs.values())
    return Candidate(candidate_id=identifier(), account_id=account, content_id=raw["id"], content_type=kind,
        author_id=author_id, author=users.get(author_id), text=(raw.get("note_post") or raw.get("note_tweet") or {}).get("text", raw.get("text")),
        created_at=raw.get("created_at"), reply_to=refs.get("replied_to"), quote_id=refs.get("quoted"),
        repost_of=refs.get("retweeted"), conversation_id=raw.get("dm_conversation_id" if dm else "conversation_id"),
        participant_ids=raw.get("participant_ids", []), engagement=raw.get("public_metrics"),
        media=[media[k] for k in raw.get("attachments", {}).get("media_keys", []) if k in media],
        related_posts=[p for p in includes.get("posts", includes.get("tweets", [])) if p["id"] in related_ids],
        url=None if dm else "https://x.com/i/status/"+raw["id"], observed_at=time.time(), source=source).model_dump(mode="json")


def controlled(account, candidate):
    # Conservative DM policy: only sent messages. Incoming deletion semantics are
    # not assumed from a public post ownership rule or client-supplied participants.
    return candidate.get("author_id") == account


def protection_reason(candidate, policy, pinned=None):
    ids = {candidate["content_id"], candidate.get("repost_of")}
    if ids & set(policy.protected_ids):
        return "protected_id"
    if candidate["content_type"] == "DM" and candidate.get("conversation_id") in policy.protected_dm_conversations:
        return "protected_conversation"
    if policy.protect_pinned and pinned and pinned in ids:
        return "pinned_post"
    if policy.min_age_seconds:
        if not candidate.get("created_at"):
            return "age_unavailable"
        created = datetime.fromisoformat(candidate["created_at"].replace("Z", "+00:00"))
        if created.tzinfo is None or (datetime.now(timezone.utc)-created).total_seconds() < policy.min_age_seconds:
            return "too_recent"
    for metric, threshold in policy.engagement_thresholds.items():
        metrics = candidate.get("engagement") or {}
        if metric not in metrics:
            return "engagement_unavailable"
        if metrics[metric] >= threshold:
            return "engagement_threshold"
    return None


class Cleanup:
    def __init__(self, store, backend_factory=None, live_enabled=None):
        self.store, self.repo = store, CleanupRepository(store)
        self.oauth = XOAuth(self.repo)
        self.backend_mode = os.environ.get("XP_CLEANUP_BACKEND", "session")
        if self.backend_mode not in {"session", "official"}:
            raise ValueError("XP_CLEANUP_BACKEND must be session or official")
        async def session_backend(account):
            return SessionCleanup(self.store, account)
        self.backend_factory = backend_factory or (session_backend if self.backend_mode == "session" else self.oauth.backend)
        self.live_enabled = os.environ.get("XP_ENABLE_LIVE_CLEANUP", "false").lower() == "true" if live_enabled is None else live_enabled
        self.max_plan_actions = min(100000, max(1, int(os.environ.get("XP_MAX_PLAN_ACTIONS", "10000"))))
        self.max_run_actions = min(50, max(1, int(os.environ.get("XP_MAX_RUN_ACTIONS", "25"))))

    def readiness(self, account):
        self.store.account(account)
        row = self.repo.db.execute("SELECT value FROM credentials WHERE account=?", (account,)).fetchone()
        scopes = set(json.loads(self.store.cipher.decrypt(row[0])).get("scope", "").split()) if row else set()
        return {"account_id": account, "backend": self.backend_mode,
                "session_connected": bool(self.store.session(account)) if self.backend_mode == "session" else False,
                "developer_api_required": self.backend_mode == "official",
                "supported_actions": ["DELETE_POST", "UNDO_REPOST"] if self.backend_mode == "session" else list(set(ACTION_FOR.values())),
                "supported_collections": ["posts", "replies"] if self.backend_mode == "session" else ["posts", "archive_posts", "dms"],
                "official_x_connected": bool(row), "x_scopes": sorted(scopes),
                "live_execution_enabled": self.live_enabled,
                "execution_authorization": "authenticated_live_request_for_exact_plan",
                "max_plan_actions": self.max_plan_actions,
                "max_actions_per_run": self.max_run_actions, "concurrency": 1,
                "archive_search": "unavailable_on_session_backend" if self.backend_mode == "session" else "optional_requires_app_entitlement; authored_posts_only",
                "dm_policy": "unavailable_on_session_backend" if self.backend_mode == "session" else "sent_MessageCreate_only", "pinned_detection": "lookup_required_fail_closed"}

    async def verified_backend(self, account):
        self.store.account(account)
        backend = await self.backend_factory(account)
        if _backends.get() is not None:
            _backends.get().append(backend)
        try:
            identity = await backend.identity()
        except Problem as exc:
            # Distinguish a safe, pre-dispatch verification failure from any
            # error after a mutation intent. Runners may back off on the former.
            exc.cleanup_phase = "identity_verification"
            raise
        if identity["id"] != account:
            raise Problem("identity_mismatch", "Cleanup connection belongs to a different account")
        return backend

    @backend_lifetime
    async def scan(self, account, kind="posts", cursor=None, limit=100):
        minimum = {"posts": 5, "replies": 5, "dms": 1, "archive_posts": 10}.get(kind)
        if minimum is None or not minimum <= limit <= 100:
            raise Problem("invalid_scan", "Use posts/replies (5–100), archive_posts (10–100), or dms (1–100) per page")
        token = None
        if cursor:
            row = self.repo.db.execute("SELECT * FROM scan_cursors WHERE id=? AND account=? AND kind=? AND expires>?",
                (cursor, account, kind, time.time())).fetchone()
            if not row:
                raise Problem("invalid_cursor", "Cursor is expired or belongs to another account/content collection")
            token = row["token"]
        backend = await self.verified_backend(account)
        result = await backend.scan(account, kind, token, limit)
        candidates = []
        for raw in result.get("data", []):
            if kind == "dms" and raw.get("event_type") != "MessageCreate":
                continue
            c = normalize(account, raw, result.get("includes"), kind == "dms", getattr(backend, 'source', 'x_api_v2'))
            candidates.append(c)
        next_token = result.get("meta", {}).get("next_token")
        next_cursor = identifier() if next_token else None
        with self.repo.db:
            for c in candidates:
                self.repo.db.execute("INSERT INTO candidates VALUES (?,?,?,?)", (c["candidate_id"], account, canonical(c), time.time()))
            if next_cursor:
                self.repo.db.execute("INSERT INTO scan_cursors VALUES (?,?,?,?,?)", (next_cursor, account, kind, next_token, time.time()+86400))
        return {"schema_version": "1", "account_id": account, "candidates": candidates, "next_cursor": next_cursor,
                "collection": kind, "rate_limit": backend.rate,
                "history_limit": getattr(backend, 'history_limit', {"dms": "30_days", "posts": "latest_3200_timeline_items",
                    "archive_posts": "full_archive_subject_to_entitlement_and_search_visibility; excludes_reposts"}.get(kind, "profile_timeline_visibility")),
                "complete_account_history": False}

    def stage(self, account, proposals):
        self.store.account(account)
        if not 1 <= len(proposals) <= 500:
            raise Problem("invalid_batch_size", "Stage 1–500 typed proposals per batch")
        # Resolve account-bound provenance now; do not accept arbitrary IDs/content.
        for proposal in proposals:
            self.repo.candidate(account, proposal.candidate_id)
        bid = identifier()
        with self.repo.db:
            self.repo.db.execute("INSERT INTO batches VALUES (?,?,?,?)", (bid, account,
                canonical([p.model_dump() for p in proposals]), time.time()))
        return {"batch_id": bid, "account_id": account, "count": len(proposals), "deleted": False}

    @backend_lifetime
    async def preview(self, account, proposals=None, batch_ids=None):
        self.store.account(account)
        proposals = list(proposals or [])
        if len(proposals) > 500 or len(batch_ids or []) > 200:
            raise Problem("invalid_batch_size", "Use at most 500 inline actions or 200 staged batch IDs")
        for bid in batch_ids or []:
            row = self.repo.db.execute("SELECT payload FROM batches WHERE id=? AND account=?", (bid, account)).fetchone()
            if not row:
                raise Problem("batch_unavailable", "Proposal batch does not belong to this account")
            proposals.extend(ProposedAction.model_validate(p) for p in json.loads(row[0]))
            if len(proposals) > self.max_plan_actions:
                raise Problem("plan_limit", "Proposed action count exceeds configured plan limit")
        if len(proposals) > self.max_plan_actions:
            raise Problem("plan_limit", "Proposed action count exceeds configured plan limit")
        policy = self.repo.policy(account)
        # Avoid remote reads for empty plans. Nonempty plans verify the real grant.
        backend = await self.verified_backend(account) if proposals else None
        has_posts = any(self.repo.candidate(account, p.candidate_id)["content_type"] != "DM" for p in proposals)
        pinned = await backend.pinned(account) if backend and policy.protect_pinned and has_posts else None
        actions, failures, skipped, seen = [], [], [], set()
        for proposal in proposals:
            try:
                c = self.repo.candidate(account, proposal.candidate_id)
            except Problem as exc:
                failures.append({"candidate_id": proposal.candidate_id, "code": exc.code})
                continue
            target = c.get("repost_of") if c["content_type"] == "REPOST" else c["content_id"]
            key = (proposal.action, target)
            reason = None
            if c['source'] in LOCAL_OBSERVATION_SOURCES:
                try:
                    self.repo.verify_browser_observation(account, c)
                    if not isinstance(backend, SessionCleanup):
                        raise Problem('browser_observation_unsupported', 'Browser-observed targets require the session backend')
                except Problem as exc:
                    failures.append({'candidate_id': c['candidate_id'], 'code': exc.code})
                    continue
            if proposal.decision and proposal.decision.label != "DELETE":
                reason = "decision_"+proposal.decision.label.lower()
            elif proposal.action != ACTION_FOR[c["content_type"]]:
                failures.append({"candidate_id": c["candidate_id"], "code": "action_type_mismatch"})
                continue
            elif not controlled(account, c):
                failures.append({"candidate_id": c["candidate_id"], "code": "not_owned"})
                continue
            elif not target or not target.isdigit():
                failures.append({"candidate_id": c["candidate_id"], "code": "invalid_target"})
                continue
            elif key in seen:
                reason = "duplicate"
            else:
                reason = protection_reason(c, policy, pinned)
            seen.add(key)
            if reason:
                skipped.append({"candidate_id": c["candidate_id"], "target_id": target, "code": reason})
                continue
            # Scope checks are deterministic; they do not decide what deserves deletion.
            from .x_api import DM_WRITE_SCOPES, POST_SCOPES
            try:
                if hasattr(backend, 'require_action'):
                    backend.require_action(proposal.action)
                backend.require(DM_WRITE_SCOPES if proposal.action == "DELETE_DM" else POST_SCOPES)
            except Problem as exc:
                failures.append({"candidate_id": c["candidate_id"], "code": exc.code})
                continue
            actions.append({"candidate": c, "target_id": target, "action": proposal.action,
                            "decision": proposal.decision.model_dump() if proposal.decision else None})
        pid = identifier()
        header = {"schema_version": "1", "plan_id": pid, "account_id": account, "total_actions": len(actions),
            "counts_by_action": dict(Counter(a["action"] for a in actions)),
            "counts_by_content": dict(Counter(a["candidate"]["content_type"] for a in actions)),
            "validation_failures": failures, "protected_or_skipped": skipped, "policy": policy.model_dump(),
            "validation_source": ("authenticated_session_identity_and_locally_attested_browser_or_public_x_view; attestation_revalidated_before_dispatch"
                if any(a['candidate']['source'] in LOCAL_OBSERVATION_SOURCES for a in actions)
                else "authenticated_"+getattr(backend, 'source', 'x_api_v2')+"_snapshots; live_revalidation_before_each_action"),
            "created_at": time.time()}
        digest = hashlib.sha256(canonical({"plan": header, "actions": actions}).encode()).hexdigest()
        with self.repo.db:
            self.repo.db.execute("INSERT INTO plans VALUES (?,?,?,?,?)", (pid, account, canonical(header), digest, time.time()))
            for seq, action in enumerate(actions):
                self.repo.db.execute("INSERT INTO actions VALUES (?,?,?)", (pid, seq, canonical(action)))
                self.repo.db.execute("INSERT INTO progress(plan,seq,state) VALUES (?,?,'pending')", (pid, seq))
            self.repo.db.execute("INSERT INTO plan_seals VALUES (?)", (pid,))
        return self.status(account, pid)

    def status(self, account, pid, cursor=0, limit=100):
        if cursor < 0 or not 1 <= limit <= 500:
            raise Problem("invalid_page", "Use a nonnegative cursor and 1–500 results")
        row = self.repo.plan(account, pid)
        header = json.loads(row["payload"])
        counts = dict(self.repo.db.execute("SELECT state,COUNT(*) FROM progress WHERE plan=? GROUP BY state", (pid,)))
        items = self.repo.db.execute('''SELECT a.seq,a.payload,p.state,p.attempts,p.retry_at,p.receipt FROM actions a
            JOIN progress p ON p.plan=a.plan AND p.seq=a.seq WHERE a.plan=? AND a.seq>=? ORDER BY a.seq LIMIT ?''', (pid, cursor, limit)).fetchall()
        pending = self.repo.db.execute("SELECT MIN(seq) FROM progress WHERE plan=? AND state='pending'", (pid,)).fetchone()[0]
        retry = self.repo.db.execute("SELECT MAX(retry_at) FROM progress WHERE plan=? AND state='pending'", (pid,)).fetchone()[0]
        failures, skipped = header.pop("validation_failures"), header.pop("protected_or_skipped")
        page_total = max(header["total_actions"], len(failures), len(skipped))
        return {**header, "validation_failures": failures[cursor:cursor+limit], "validation_failure_count": len(failures),
            "protected_or_skipped": skipped[cursor:cursor+limit], "protected_or_skipped_count": len(skipped),
            "plan_digest": row["digest"], "live_execution_enabled": self.live_enabled,
            "progress": {k: counts.get(k, 0) for k in ["pending", "inflight", "succeeded", "skipped", "failed", "unknown"]},
            "current_cursor": pending, "retry_at": retry or None,
            "items": [{"seq": r["seq"], **json.loads(r["payload"]), "state": r["state"], "attempts": r["attempts"],
                       "retry_at": r["retry_at"], "receipt": json.loads(r["receipt"])} for r in items],
            "next_cursor": cursor+limit if cursor+limit < page_total else None}

    def audit_history(self, account, after=0, limit=100):
        if after < 0 or not 1 <= limit <= 500:
            raise Problem("invalid_page", "Use 1–500 audit results")
        rows = self.repo.db.execute("SELECT * FROM audit WHERE account=? AND id>? ORDER BY id LIMIT ?", (account, after, limit)).fetchall()
        return {"account_id": account, "items": [{**dict(r), "decision": json.loads(r["decision"]),
            "receipt": json.loads(r["receipt"])} for r in rows], "next_cursor": rows[-1]["id"] if rows else None}

    def record(self, account, pid, seq, action, state, receipt, retry_at=0):
        with self.repo.db:
            self._record(account, pid, seq, action, state, receipt, retry_at)

    def _record(self, account, pid, seq, action, state, receipt, retry_at=0):
        c = action["candidate"]
        self.repo.db.execute("UPDATE progress SET state=?,receipt=?,retry_at=? WHERE plan=? AND seq=?",
            (state, canonical(receipt), retry_at, pid, seq))
        self.repo.db.execute("INSERT INTO audit(account,plan,seq,target,content_type,action,decision,timestamp,result,receipt) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (account, pid, seq, action["target_id"], c["content_type"], action["action"], canonical(action["decision"]), time.time(), state, canonical(receipt)))
        if state in {"pending", "failed", "skipped"}:
            self.repo.db.execute("DELETE FROM target_claims WHERE account=? AND action=? AND target=? AND plan=? AND seq=?",
                (account, action["action"], action["target_id"], pid, seq))

    @backend_lifetime
    async def execute(self, account, pid, key, dry_run=True, max_actions=25, cursor=0, *, client_authorized=False):
        row = self.repo.plan(account, pid)
        frozen = [json.loads(r[0]) for r in self.repo.db.execute("SELECT payload FROM actions WHERE plan=? ORDER BY seq", (pid,))]
        expected = hashlib.sha256(canonical({"plan": json.loads(row["payload"]), "actions": frozen}).encode()).hexdigest()
        if expected != row["digest"]:
            raise Problem("plan_integrity_failed", "Frozen plan digest does not match its stored targets")
        if not 8 <= len(key) <= 128 or not 1 <= max_actions <= self.max_run_actions:
            raise Problem("invalid_execution", "Use a stable 8–128 character key and configured run action limit")
        mode = "dry_run" if dry_run else "live"
        with self.repo.db:
            old = self.repo.db.execute("SELECT * FROM execution_keys WHERE account=? AND key=?", (account, key)).fetchone()
            if old and (old["plan"] != pid or old["mode"] != mode):
                raise Problem("idempotency_conflict", "Request key is already bound to another plan or execution mode")
            self.repo.db.execute("INSERT OR IGNORE INTO execution_keys VALUES (?,?,?,?)", (account, key, pid, mode))
        if dry_run:
            status = self.status(account, pid, cursor, max_actions)
            original = ProtectionPolicy.model_validate(json.loads(row["payload"])["policy"])
            current = self.repo.policy(account)
            would_execute, review_skipped = [], []
            for item in status["items"]:
                reason = ("state_"+item["state"] if item["state"] != "pending" else
                          protection_reason(item["candidate"], original) or protection_reason(item["candidate"], current))
                if reason:
                    review_skipped.append({"seq": item["seq"], "code": reason})
                else:
                    would_execute.append({"seq": item["seq"], "action": item["action"], "target_id": item["target_id"]})
            return {**status, "dry_run": True, "would_execute": [{"seq": i["seq"], "action": i["action"], "target_id": i["target_id"]}
                for i in would_execute], "review_skipped": review_skipped,
                "review_basis": ("frozen_browser_observation_and_current_local_protections; local_attestation_and_session_identity_required"
                    if any(i['candidate']['source'] in LOCAL_OBSERVATION_SOURCES for i in status['items'])
                    else "frozen_snapshot_and_current_local_protections; live_X_revalidation_required"),
                "remote_mutations": 0, "progress_consumed": False}
        if not self.live_enabled:
            raise Problem("live_cleanup_disabled", "Live cleanup is disabled by server configuration")
        if client_authorized:
            # The MCP route has already checked cleanup:execute and this account.
            # Its explicit live request authorizes only this immutable plan.
            # Keep short-lived digest permits for budgets and mid-run revocation,
            # without requiring a second administrator authentication step.
            with self.repo.db:
                self.repo.db.execute("INSERT OR REPLACE INTO approvals VALUES (?,?,?,?)", (
                    pid, row["digest"], time.time() + 600, len(frozen)))
        approval = self.repo.db.execute("SELECT * FROM approvals WHERE plan=?", (pid,)).fetchone()
        if not approval or approval["digest"] != row["digest"] or approval["expires"] <= time.time():
            raise Problem("plan_not_approved", "Administrator must approve this exact plan digest after owner review")
        backend = await self.verified_backend(account)
        owner = identifier()
        with self.repo.db:
            self.repo.db.execute("BEGIN IMMEDIATE")
            # One account runner across processes/plans. Expired leases leave
            # in-flight work UNKNOWN, never automatically replayed.
            active = self.repo.db.execute("SELECT r.* FROM runs r JOIN plans p ON p.id=r.plan WHERE p.account=? AND r.lease>?", (account, time.time())).fetchone()
            if active:
                raise Problem("cleanup_busy", "This account has an active cleanup run")
            expired = self.repo.db.execute("SELECT r.plan FROM runs r JOIN plans p ON p.id=r.plan WHERE p.account=?", (account,)).fetchall()
            for previous in expired:
                for interrupted in self.repo.db.execute("SELECT a.seq,a.payload FROM actions a JOIN progress p ON p.plan=a.plan AND p.seq=a.seq WHERE a.plan=? AND p.state='inflight'", (previous[0],)).fetchall():
                    self._record(account, previous[0], interrupted[0], json.loads(interrupted[1]), "unknown", {"code": "interrupted_unknown"})
                self.repo.db.execute("DELETE FROM runs WHERE plan=?", (previous[0],))
            self.repo.db.execute("INSERT INTO runs VALUES (?,?,?)", (pid, owner, time.time()+120))
        attempts, started = 0, time.monotonic()
        try:
            while attempts < max_actions and time.monotonic()-started < 30:
                self.store.account(account)
                approval = self.repo.db.execute("SELECT * FROM approvals WHERE plan=?", (pid,)).fetchone()
                if not approval or approval["digest"] != row["digest"] or time.time() >= approval["expires"]:
                    break
                # The audit log is append-only and retains historical intent
                # rows. Count terminal action state instead, so completed
                # batches consume the approval budget once while old intent
                # records do not exhaust it prematurely.
                spent = self.repo.db.execute("SELECT COUNT(*) FROM progress WHERE plan=? AND state IN ('succeeded','skipped','failed','unknown')", (pid,)).fetchone()[0]
                if spent >= approval["max_actions"]:
                    break
                r = self.repo.db.execute('''SELECT a.seq,a.payload,p.attempts,p.retry_at FROM actions a JOIN progress p
                    ON p.plan=a.plan AND p.seq=a.seq WHERE a.plan=? AND p.state='pending' ORDER BY a.seq LIMIT 1''', (pid,)).fetchone()
                if not r or r["retry_at"] > time.time():
                    break
                seq, action = r["seq"], json.loads(r["payload"])
                attempts += 1
                submitted = False
                with self.repo.db:
                    self.repo.db.execute("UPDATE runs SET lease=? WHERE plan=? AND owner=?", (time.time()+120, pid, owner))
                    self.repo.db.execute("UPDATE progress SET attempts=attempts+1 WHERE plan=? AND seq=?", (pid, seq))
                try:
                    # Verify remote snapshots again, or the private administrator's
                    # short-lived browser observation when the remote lookup fails.
                    browser_reviewed = action['candidate']['source'] in LOCAL_OBSERVATION_SOURCES
                    if browser_reviewed:
                        self.repo.verify_browser_observation(account, action['candidate'])
                        if not isinstance(backend, SessionCleanup) or action['action'] != 'DELETE_POST':
                            raise Problem('browser_observation_unsupported', 'Browser observations support exact authored-post deletion through the session backend')
                        fresh = action['candidate']
                    else:
                        lookup = await backend.lookup(action["candidate"])
                        raw = lookup["data"]
                        if not isinstance(raw, dict) or raw.get("id") != action["candidate"]["content_id"]:
                            raise Problem("target_mismatch", "X read-back returned a different target")
                        if action["candidate"]["content_type"] == "DM" and raw.get("event_type") != "MessageCreate":
                            raise Problem("target_mismatch", "DM event is not a message")
                        fresh = normalize(account, raw, lookup.get("includes"), action["candidate"]["content_type"] == "DM", getattr(backend, 'source', 'x_api_v2'))
                    current_target = fresh.get("repost_of") if fresh["content_type"] == "REPOST" else fresh["content_id"]
                    if not controlled(account, fresh) or current_target != action["target_id"] or fresh["content_type"] != action["candidate"]["content_type"]:
                        raise Problem("target_mismatch", "Ownership or target type changed since preview")
                    # Original and current policies both apply. Changes can add
                    # protection but cannot weaken the protections frozen in a plan.
                    original = ProtectionPolicy.model_validate(json.loads(row["payload"])["policy"])
                    current = self.repo.policy(account)
                    pinned = await backend.pinned(account) if fresh["content_type"] != "DM" and (original.protect_pinned or current.protect_pinned) else None
                    reason = protection_reason(fresh, original, pinned) or protection_reason(fresh, current, pinned)
                    if reason:
                        self.record(account, pid, seq, action, "skipped", {"code": reason})
                        continue
                    with self.repo.db:
                        # Approval may have been revoked/expired while read-back
                        # requests were in flight. Recheck immediately before intent.
                        permit = self.repo.db.execute("SELECT * FROM approvals WHERE plan=?", (pid,)).fetchone()
                        if not permit or permit["digest"] != row["digest"] or permit["expires"] <= time.time():
                            break
                        claim = self.repo.db.execute("INSERT OR IGNORE INTO target_claims VALUES (?,?,?,?,?)",
                            (account, action["action"], action["target_id"], pid, seq))
                        if claim.rowcount == 0:
                            self._record(account, pid, seq, action, "skipped", {"code": "target_already_claimed"})
                            continue
                        self._record(account, pid, seq, action, "inflight", {"code": "request_intent"})
                    submitted = True
                    if browser_reviewed:
                        receipt = await backend.execute_browser_verified(account, action['action'], action['target_id'])
                    elif action['action'] == 'UNDO_REPOST' and isinstance(backend, SessionCleanup):
                        receipt = await backend.execute_repost(account, action['target_id'], action['candidate']['content_id'])
                    else:
                        receipt = await backend.execute(account, action["action"], action["target_id"])
                    self.record(account, pid, seq, action, "unknown" if receipt.get('requires_owner_verification') else "succeeded", receipt)
                except asyncio.CancelledError:
                    self.record(account, pid, seq, action, "unknown" if submitted else "pending", {"code": "interrupted_unknown" if submitted else "interrupted_before_submit"})
                    raise
                except XAPIError as exc:
                    receipt = {"code": exc.code, "http_status": exc.status, "rate_limit": exc.rate}
                    if exc.code in {"rate_limited", "transport_error", "transient_x_error"}:
                        state = "failed" if r["attempts"] >= 4 else "pending"
                        self.record(account, pid, seq, action, state, receipt, exc.retry_at or time.time()+30)
                        break
                    state = "unknown" if submitted and exc.code in {"transport_unknown", "remote_unknown"} else "failed"
                    # Missing before dispatch is a safe skip, never a fabricated success.
                    if exc.code == "x_not_found" and not submitted:
                        state = "skipped"
                    self.record(account, pid, seq, action, state, receipt)
                except Exception as exc:
                    code = exc.code if isinstance(exc, Problem) else "cleanup_error"
                    self.record(account, pid, seq, action, "unknown" if submitted else "failed", {"code": code})
        finally:
            with self.repo.db:
                self.repo.db.execute("DELETE FROM runs WHERE plan=? AND owner=?", (pid, owner))
        return {**self.status(account, pid), "dry_run": False, "actions_processed_this_run": attempts,
                "request_rate_limits": getattr(backend, 'request_rates', {})}
