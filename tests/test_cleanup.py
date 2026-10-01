import asyncio
import json
import sqlite3
import time

from cryptography.fernet import Fernet
import httpx
from pydantic import ValidationError
import pytest

from x_publisher.backend import capability_defaults
from x_publisher.cleanup import ACTION_FOR, Cleanup, normalize
from x_publisher.cleanup_models import Candidate, Decision, ProposedAction, ProtectionPolicy
from x_publisher.core import Problem, Store, canonical
from x_publisher.x_api import DM_WRITE_SCOPES, OfficialX, XAPIError, XOAuth, CALLBACK


class MockX:
    def __init__(self):
        self.posts = {}
        self.calls = []
        self.scopes = DM_WRITE_SCOPES | {"tweet.write"}
        self.rate = {}
        self.pin = None
        self.failure = {}
        self.lookup_failure = {}
        self.identity_id = "1"
        self.next_token = None
        self.scan_tokens = []

    async def identity(self):
        return {"id": self.identity_id}

    def require(self, scopes):
        if not set(scopes) <= self.scopes:
            raise Problem("missing_x_scopes", "Required official scope missing")

    async def pinned(self, account):
        return self.pin

    async def scan(self, account, kind, cursor, limit):
        self.scan_tokens.append(cursor)
        return {"data": list(self.posts.values())[:limit], "meta": {"next_token": self.next_token}}

    async def lookup(self, candidate):
        cid = candidate["content_id"]
        if cid in self.lookup_failure:
            raise self.lookup_failure[cid]
        if cid not in self.posts:
            raise XAPIError("x_not_found", 404)
        return {"data": self.posts[cid]}

    async def execute(self, account, action, target):
        self.calls.append((account, action, target))
        if target in self.failure:
            raise self.failure[target]
        return {"http_status": 200, "data": {"retweeted": False} if action == "UNDO_REPOST" else {"deleted": True}}


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path, Fernet.generate_key())
    for aid in ["1", "2"]:
        store.save_account(aid, "user"+aid, {}, capability_defaults("free"))
    backend = MockX()
    async def factory(account):
        return backend
    cleanup = Cleanup(store, factory, live_enabled=True)
    yield cleanup, backend
    cleanup.repo.db.close()
    store.db.close()


def raw(pid="10", kind="POST", author="1"):
    r = {"id": pid, "author_id": author, "text": "untrusted post", "created_at": "2020-01-01T00:00:00Z",
         "conversation_id": "9", "public_metrics": {"like_count": 3}}
    relation = {"REPLY": "replied_to", "QUOTE": "quoted", "REPOST": "retweeted"}.get(kind)
    if relation:
        r["referenced_tweets"] = [{"type": relation, "id": "99"}]
    if kind == "DM":
        r.pop("author_id")
        r.update(sender_id=author, event_type="MessageCreate", dm_conversation_id="dm-1", participant_ids=["1", "2"])
    return r


@pytest.mark.asyncio
async def test_identity_failure_is_marked_before_dispatch(setup):
    cleanup, backend = setup
    error = XAPIError("invalid_x_response")
    async def unavailable_identity():
        raise error
    backend.identity = unavailable_identity
    with pytest.raises(XAPIError) as caught:
        await cleanup.verified_backend("1")
    assert caught.value.cleanup_phase == "identity_verification"
    assert backend.calls == []
    assert cleanup.repo.db.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 0


def candidate(setup, pid="10", kind="POST", author="1", account="1", commit=True):
    cleanup, backend = setup
    r = raw(pid, kind, author)
    backend.posts[pid] = r
    c = normalize(account, r, dm=kind == "DM")
    cleanup.repo.db.execute("INSERT INTO candidates VALUES (?,?,?,?)", (c["candidate_id"], account, canonical(c), time.time()))
    if commit:
        cleanup.repo.db.commit()
    return c


def proposal(c, decision=None, action=None):
    return ProposedAction(candidate_id=c["candidate_id"], action=action or ACTION_FOR[c["content_type"]], decision=decision)


def approve(cleanup, plan, budget=10000):
    with cleanup.repo.db:
        cleanup.repo.db.execute("INSERT OR REPLACE INTO approvals VALUES (?,?,?,?)", (plan["plan_id"], plan["plan_digest"], time.time()+600, budget))


@pytest.mark.parametrize("kind", ["POST", "REPLY", "QUOTE", "REPOST", "DM"])
async def test_each_type_plan_and_execution(setup, kind):
    cleanup, backend = setup
    c = candidate(setup, kind=kind)
    plan = await cleanup.preview("1", [proposal(c)])
    assert plan["total_actions"] == 1 and not backend.calls
    assert plan["items"][0]["candidate"]["content_type"] == kind
    approve(cleanup, plan)
    result = await cleanup.execute("1", plan["plan_id"], "test-live-key", False)
    assert result["progress"]["succeeded"] == 1
    assert backend.calls == [("1", ACTION_FOR[kind], "99" if kind == "REPOST" else "10")]
    audit = cleanup.audit_history("1")["items"]
    assert [a["result"] for a in audit] == ["inflight", "succeeded"]
    assert audit[-1]["content_type"] == kind


async def test_mixed_duplicate_unowned_wrong_type_keep_review(setup):
    cleanup, backend = setup
    cs = [candidate(setup, str(10+i), k) for i,k in enumerate(ACTION_FOR)]
    foreign = candidate(setup, "20", author="2")
    wrong = candidate(setup, "21")
    keep = candidate(setup, "22")
    review = candidate(setup, "23")
    plan = await cleanup.preview("1", [*[proposal(c) for c in cs], proposal(cs[0]), proposal(foreign),
        proposal(wrong, action="DELETE_DM"), proposal(keep, {"label": "KEEP"}), proposal(review, {"label": "REVIEW"})])
    assert plan["total_actions"] == 5
    assert {f["code"] for f in plan["validation_failures"]} == {"not_owned", "action_type_mismatch"}
    assert {f["code"] for f in plan["protected_or_skipped"]} == {"duplicate", "decision_keep", "decision_review"}


@pytest.mark.parametrize("policy,kind,code", [
    ({"protected_ids": ["10"]}, "POST", "protected_id"),
    ({"protected_ids": ["99"]}, "REPOST", "protected_id"),
    ({"protected_dm_conversations": ["dm-1"]}, "DM", "protected_conversation"),
    ({"min_age_seconds": 999999999}, "POST", "too_recent"),
    ({"engagement_thresholds": {"like_count": 2}}, "POST", "engagement_threshold"),
    ({"engagement_thresholds": {"reply_count": 100}}, "POST", "engagement_unavailable"),
])
async def test_deterministic_protections(setup, policy, kind, code):
    cleanup, backend = setup
    c = candidate(setup, kind=kind)
    cleanup.repo.set_policy("1", ProtectionPolicy(**policy))
    p = await cleanup.preview("1", [proposal(c)])
    assert p["total_actions"] == 0 and p["protected_or_skipped"][0]["code"] == code


async def test_pinned_protection(setup):
    cleanup, backend = setup
    backend.pin = "10"
    plan = await cleanup.preview("1", [proposal(candidate(setup))])
    assert plan["protected_or_skipped"][0]["code"] == "pinned_post"


async def test_protections_rechecked_and_cannot_be_weakened(setup):
    cleanup, backend = setup
    c = candidate(setup)
    plan = await cleanup.preview("1", [proposal(c)])
    approve(cleanup, plan)
    cleanup.repo.set_policy("1", ProtectionPolicy(protected_ids=["10"]))
    result = await cleanup.execute("1", plan["plan_id"], "protected-run", False)
    assert result["progress"]["skipped"] == 1 and not backend.calls
    cleanup.repo.set_policy("1", ProtectionPolicy(engagement_thresholds={"like_count": 10}))
    plan2 = await cleanup.preview("1", [proposal(c)])
    approve(cleanup, plan2)
    cleanup.repo.set_policy("1", ProtectionPolicy())
    backend.posts["10"]["public_metrics"]["like_count"] = 20
    assert (await cleanup.execute("1", plan2["plan_id"], "protected-run2", False))["progress"]["skipped"] == 1


async def test_stale_missing_and_changed_ownership(setup):
    cleanup, backend = setup
    c1, c2 = candidate(setup, "10"), candidate(setup, "11")
    plan = await cleanup.preview("1", [proposal(c1), proposal(c2)])
    approve(cleanup, plan)
    backend.posts.pop("10")
    backend.posts["11"]["author_id"] = "2"
    result = await cleanup.execute("1", plan["plan_id"], "stale-targets", False)
    assert result["progress"]["skipped"] == 1 and result["progress"]["failed"] == 1 and not backend.calls


async def test_partial_failure_and_idempotent_resume(setup):
    cleanup, backend = setup
    plan = await cleanup.preview("1", [proposal(candidate(setup, str(i))) for i in range(10, 14)])
    approve(cleanup, plan)
    backend.failure["11"] = XAPIError("x_rejected", 403)
    first = await cleanup.execute("1", plan["plan_id"], "resume-key", False, max_actions=2)
    assert first["progress"]["succeeded"] == 1 and first["progress"]["failed"] == 1 and first["progress"]["pending"] == 2
    result = await cleanup.execute("1", plan["plan_id"], "resume-key", False)
    assert result["progress"]["succeeded"] == 3 and len(backend.calls) == 4
    await cleanup.execute("1", plan["plan_id"], "resume-key", False)
    assert len(backend.calls) == 4
    # Another key cannot restart the same plan's completed work.
    await cleanup.execute("1", plan["plan_id"], "another-key", False)
    assert len(backend.calls) == 4


async def test_rate_limit_persisted_and_retry_after_reset(setup):
    cleanup, backend = setup
    plan = await cleanup.preview("1", [proposal(candidate(setup))])
    approve(cleanup, plan)
    backend.failure["10"] = XAPIError("rate_limited", 429, time.time()+900, {"remaining": 0})
    result = await cleanup.execute("1", plan["plan_id"], "rate-key", False)
    assert result["progress"]["pending"] == 1 and result["retry_at"] > time.time()
    await cleanup.execute("1", plan["plan_id"], "rate-key", False)
    assert len(backend.calls) == 1
    backend.failure.clear()
    with cleanup.repo.db:
        cleanup.repo.db.execute("UPDATE progress SET retry_at=0")
    result = await cleanup.execute("1", plan["plan_id"], "rate-key", False)
    assert result["progress"]["succeeded"] == 1 and len(backend.calls) == 2


async def test_read_retry_bounded(setup):
    cleanup, backend = setup
    plan = await cleanup.preview("1", [proposal(candidate(setup))])
    approve(cleanup, plan)
    backend.lookup_failure["10"] = XAPIError("transport_error")
    for _ in range(6):
        await cleanup.execute("1", plan["plan_id"], "read-retry-key", False)
        with cleanup.repo.db:
            cleanup.repo.db.execute("UPDATE progress SET retry_at=0")
    assert cleanup.status("1", plan["plan_id"])["progress"]["failed"] == 1
    assert cleanup.status("1", plan["plan_id"])["items"][0]["attempts"] == 5
    assert not backend.calls


@pytest.mark.parametrize("failure", [XAPIError("transport_unknown"), XAPIError("remote_unknown", 503), RuntimeError("SECRET-MUST-NOT-LEAK")])
async def test_unknown_mutation_not_replayed(setup, failure):
    cleanup, backend = setup
    c = candidate(setup)
    plan = await cleanup.preview("1", [proposal(c)])
    approve(cleanup, plan)
    backend.failure["10"] = failure
    result = await cleanup.execute("1", plan["plan_id"], "unknown-key", False)
    assert result["progress"]["unknown"] == 1
    await cleanup.execute("1", plan["plan_id"], "unknown-key", False)
    plan2 = await cleanup.preview("1", [proposal(c)])
    approve(cleanup, plan2)
    await cleanup.execute("1", plan2["plan_id"], "unknown-new-plan", False)
    assert len(backend.calls) == 1
    assert "SECRET" not in canonical(cleanup.audit_history("1"))


async def test_restart_inflight_becomes_unknown_and_pending_resumes(setup):
    cleanup, backend = setup
    plan = await cleanup.preview("1", [proposal(candidate(setup, "10")), proposal(candidate(setup, "11"))])
    approve(cleanup, plan)
    action = plan["items"][0]
    with cleanup.repo.db:
        cleanup.repo.db.execute("UPDATE progress SET state='inflight' WHERE seq=0")
        cleanup.repo.db.execute("INSERT INTO runs VALUES (?,?,?)", (plan["plan_id"], "crashed", time.time()-1))
        cleanup.repo.db.execute("INSERT INTO target_claims VALUES (?,?,?,?,?)", ("1", "DELETE_POST", "10", plan["plan_id"], 0))
    result = await cleanup.execute("1", plan["plan_id"], "restart-key", False)
    assert result["progress"]["unknown"] == 1 and result["progress"]["succeeded"] == 1
    assert backend.calls == [("1", "DELETE_POST", "11")]


async def test_cancellation_before_and_after_dispatch(setup):
    cleanup, backend = setup
    c = candidate(setup)
    plan = await cleanup.preview("1", [proposal(c)])
    approve(cleanup, plan)
    backend.lookup_failure["10"] = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await cleanup.execute("1", plan["plan_id"], "cancel-key", False)
    assert cleanup.status("1", plan["plan_id"])["progress"]["pending"] == 1
    backend.lookup_failure.clear()
    backend.failure["10"] = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await cleanup.execute("1", plan["plan_id"], "cancel-key", False)
    assert cleanup.status("1", plan["plan_id"])["progress"]["unknown"] == 1


async def test_dry_run_and_live_gates(setup):
    cleanup, backend = setup
    plan = await cleanup.preview("1", [proposal(candidate(setup))])
    result = await cleanup.execute("1", plan["plan_id"], "dry-run-key")
    assert result["remote_mutations"] == 0 and result["progress"]["pending"] == 1 and not backend.calls
    assert not cleanup.audit_history("1")["items"]
    with pytest.raises(Problem, match="already bound"):
        await cleanup.execute("1", plan["plan_id"], "dry-run-key", False)
    with pytest.raises(Problem, match="approve"):
        await cleanup.execute("1", plan["plan_id"], "live-run-key", False)
    approve(cleanup, plan)
    cleanup.live_enabled = False
    with pytest.raises(Problem, match="disabled"):
        await cleanup.execute("1", plan["plan_id"], "live-run-key", False)
    assert not backend.calls


async def test_account_bound_plan_batches_and_candidates(setup):
    cleanup, backend = setup
    c = candidate(setup)
    batch = cleanup.stage("1", [proposal(c)])
    with pytest.raises(Problem):
        cleanup.stage("2", [proposal(c)])
    with pytest.raises(Problem):
        await cleanup.preview("2", batch_ids=[batch["batch_id"]])
    plan = await cleanup.preview("1", batch_ids=[batch["batch_id"]])
    with pytest.raises(Problem):
        cleanup.status("2", plan["plan_id"])
    with pytest.raises(Problem):
        await cleanup.execute("2", plan["plan_id"], "foreign-key")
    backend.identity_id = "2"
    with pytest.raises(Problem, match="different account"):
        await cleanup.scan("1")


async def test_empty_plan(setup):
    cleanup, backend = setup
    plan = await cleanup.preview("1")
    assert plan["total_actions"] == 0 and plan["items"] == []
    assert (await cleanup.execute("1", plan["plan_id"], "empty-dry-key"))["would_execute"] == []


async def test_ten_thousand_actions_batched_persistent_pagination(setup):
    cleanup, backend = setup
    batch_ids = []
    for start in range(0, 10000, 500):
        with cleanup.repo.db:
            actions = [proposal(candidate(setup, str(100000+i), commit=False)) for i in range(start, start+500)]
        batch_ids.append(cleanup.stage("1", actions)["batch_id"])
    plan = await cleanup.preview("1", batch_ids=batch_ids)
    assert plan["total_actions"] == 10000 and len(plan["items"]) == 100
    last = cleanup.status("1", plan["plan_id"], 9999)
    assert last["items"][0]["target_id"] == "109999" and last["next_cursor"] is None
    approve(cleanup, plan)
    first = await cleanup.execute("1", plan["plan_id"], "large-plan-key", False, max_actions=2)
    assert first["current_cursor"] == 2
    second = Cleanup(cleanup.store, cleanup.backend_factory, live_enabled=True)
    try:
        resumed = await second.execute("1", plan["plan_id"], "large-plan-key", False, max_actions=2)
        assert resumed["current_cursor"] == 4 and len(backend.calls) == 4
    finally:
        second.repo.db.close()
    with pytest.raises(Problem, match="limit"):
        await cleanup.preview("1", batch_ids=batch_ids+[batch_ids[0]])


async def test_scan_cursors_metadata_and_missing_data(setup):
    cleanup, backend = setup
    backend.posts["10"] = raw()
    backend.next_token = "upstream-secret-cursor"
    first = await cleanup.scan("1", limit=5)
    second = await cleanup.scan("1", cursor=first["next_cursor"], limit=5)
    assert backend.scan_tokens == [None, "upstream-secret-cursor"]
    assert first["complete_account_history"] is False
    with pytest.raises(Problem):
        await cleanup.scan("2", cursor=first["next_cursor"])
    with pytest.raises(Problem):
        await cleanup.scan("1", "dms", first["next_cursor"])
    minimal = normalize("1", {"id": "1"})
    assert minimal["text"] is None and minimal["created_at"] is None and minimal["engagement"] is None
    assert Candidate.model_validate(first["candidates"][0])


def test_typed_decisions_and_invalid_action():
    d = Decision(label="DELETE", probabilities={"KEEP": .02, "DELETE": .96, "REVIEW": .02}, engine="JEV")
    assert d.probabilities.DELETE == .96
    for probabilities in [{"KEEP": 0, "DELETE": .9, "REVIEW": .2}, {"KEEP": 0, "DELETE": float("nan"), "REVIEW": 0}]:
        with pytest.raises(ValidationError):
            Decision(label="DELETE", probabilities=probabilities)
    with pytest.raises(ValidationError):
        ProposedAction(candidate_id="x", action="DELETE_EVERYTHING")


async def test_immutability_and_digest_integrity(setup):
    cleanup, backend = setup
    plan = await cleanup.preview("1", [proposal(candidate(setup))])
    for query in ["UPDATE plans SET payload='{}'", "UPDATE actions SET payload='{}'", "DELETE FROM plans", "DELETE FROM actions"]:
        with pytest.raises(sqlite3.IntegrityError):
            with cleanup.repo.db:
                cleanup.repo.db.execute(query)
    # Even an inserted extra row is caught by digest verification.
    with pytest.raises(sqlite3.IntegrityError):
        with cleanup.repo.db:
            cleanup.repo.db.execute("INSERT INTO actions VALUES (?,?,?)", (plan["plan_id"], 99, canonical(plan["items"][0])))
    with cleanup.repo.db:
        cleanup.repo.db.execute("DROP TRIGGER frozen_action_insert")
        cleanup.repo.db.execute("INSERT INTO actions VALUES (?,?,?)", (plan["plan_id"], 99, canonical(plan["items"][0])))
    with pytest.raises(Problem, match="digest"):
        await cleanup.execute("1", plan["plan_id"], "tampered-key")


async def test_approval_budget_and_expiry(setup):
    cleanup, backend = setup
    plan = await cleanup.preview("1", [proposal(candidate(setup, str(i))) for i in range(10, 13)])
    approve(cleanup, plan, 1)
    result = await cleanup.execute("1", plan["plan_id"], "budget-key", False)
    assert result["progress"]["succeeded"] == 1 and result["progress"]["pending"] == 2
    await cleanup.execute("1", plan["plan_id"], "budget-key2", False)
    assert len(backend.calls) == 1
    with cleanup.repo.db:
        cleanup.repo.db.execute("UPDATE approvals SET expires=0")
    with pytest.raises(Problem, match="approve"):
        await cleanup.execute("1", plan["plan_id"], "expired-key", False)


async def test_account_concurrency_gate(setup):
    cleanup, backend = setup
    plan = await cleanup.preview("1", [proposal(candidate(setup))])
    approve(cleanup, plan)
    with cleanup.repo.db:
        cleanup.repo.db.execute("INSERT INTO runs VALUES (?,?,?)", (plan["plan_id"], "other-worker", time.time()+120))
    with pytest.raises(Problem, match="active cleanup"):
        await cleanup.execute("1", plan["plan_id"], "busy-key", False)
    assert not backend.calls


@pytest.mark.parametrize("action,path,data", [
    ("DELETE_POST", "/2/tweets/99", {"deleted": True}),
    ("UNDO_REPOST", "/2/users/1/retweets/99", {"retweeted": False}),
    ("DELETE_DM", "/2/dm_events/99", {"deleted": True}),
])
async def test_official_endpoint_dispatch(action, path, data):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"data": data})
    def factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)
    backend = OfficialX("SECRET_TOKEN", DM_WRITE_SCOPES | {"tweet.write"}, factory)
    result = await backend.execute("1", action, "99")
    assert requests[0].url.path == path and requests[0].method == "DELETE"
    assert "SECRET_TOKEN" not in canonical(result)


@pytest.mark.parametrize("status,data,code", [(429, {}, "rate_limited"), (503, {}, "remote_unknown"),
    (403, {}, "x_rejected"), (200, {"data": {"deleted": False}}, "remote_unknown"),
    (200, {"data": {"deleted": True}, "errors": [{"detail": "secret"}]}, "remote_unknown")])
async def test_official_response_classification(status, data, code):
    def factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(status, json=data,
            headers={"x-rate-limit-reset": str(int(time.time()+900))})), **kwargs)
    backend = OfficialX("secret", {"tweet.read", "users.read", "tweet.write"}, factory)
    with pytest.raises(XAPIError) as error:
        await backend.execute("1", "DELETE_POST", "99")
    assert error.value.code == code
    assert "secret" not in str(error.value)


async def test_official_oauth_pkce_identity_encryption_refresh_and_replay(setup):
    cleanup, backend = setup
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "OFFICIAL_SECRET", "refresh_token": "REFRESH_SECRET",
                "scope": "tweet.read users.read tweet.write offline.access", "expires_in": 7200})
        return httpx.Response(200, json={"data": {"id": "1"}})
    def factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)
    oauth = XOAuth(cleanup.repo, factory)
    oauth.save_app("APP_ID", "APP_SECRET")
    from urllib.parse import parse_qs, urlsplit
    query = parse_qs(urlsplit(oauth.begin("1")).query)
    assert query["code_challenge_method"] == ["S256"] and query["redirect_uri"] == [CALLBACK]
    state = query["state"][0]
    result = await oauth.complete(state, "AUTH_CODE")
    assert result["connected"]
    stored = cleanup.repo.db.execute("SELECT value FROM credentials WHERE account='1'").fetchone()[0]
    assert b"OFFICIAL_SECRET" not in stored and b"REFRESH_SECRET" not in stored
    assert "code_verifier=" in requests[0].content.decode()
    with pytest.raises(Problem, match="expired"):
        await oauth.complete(state, "AUTH_CODE")
    token = json.loads(cleanup.store.cipher.decrypt(stored))
    token["expires_at"] = 0
    cleanup.repo.save_credential("1", token)
    await oauth.backend("1")
    assert "grant_type=refresh_token" in requests[-1].content.decode()


async def test_cookie_sessions_cannot_authorize_official_cleanup(setup):
    cleanup, backend = setup
    assert not cleanup.readiness("1")["official_x_connected"]
    with pytest.raises(Problem, match="official X OAuth"):
        await cleanup.oauth.backend("1")


async def test_missing_scopes_and_received_dm_rejected(setup):
    cleanup, backend = setup
    incoming = candidate(setup, "10", "DM", "2")
    sent = candidate(setup, "11", "DM")
    backend.scopes = {"tweet.read", "users.read", "tweet.write"}
    plan = await cleanup.preview("1", [proposal(incoming), proposal(sent)])
    assert {f["code"] for f in plan["validation_failures"]} == {"not_owned", "missing_x_scopes"}


async def test_approval_revoked_during_readback_blocks_dispatch(setup):
    cleanup, backend = setup
    plan = await cleanup.preview("1", [proposal(candidate(setup))])
    approve(cleanup, plan)
    original_lookup = backend.lookup
    async def revoked_lookup(c):
        with cleanup.repo.db:
            cleanup.repo.db.execute("DELETE FROM approvals")
        return await original_lookup(c)
    backend.lookup = revoked_lookup
    result = await cleanup.execute("1", plan["plan_id"], "revoked-during-read", False)
    assert result["progress"]["pending"] == 1 and backend.calls == []


async def test_exclusions_and_failures_are_paginated(setup):
    cleanup, backend = setup
    actions = []
    with cleanup.repo.db:
        for i in range(250):
            actions.append(proposal(candidate(setup, str(100+i), author="2", commit=False)))
    plan = await cleanup.preview("1", actions)
    assert plan["validation_failure_count"] == 250 and len(plan["validation_failures"]) == 100
    assert plan["next_cursor"] == 100
    last = cleanup.status("1", plan["plan_id"], cursor=200)
    assert len(last["validation_failures"]) == 50 and last["next_cursor"] is None


async def test_pin_lookup_failure_never_silently_disables_protection(setup):
    cleanup, backend = setup
    async def fail(account):
        raise XAPIError("x_rejected", 400)
    backend.pinned = fail
    with pytest.raises(XAPIError):
        await cleanup.preview("1", [proposal(candidate(setup))])
    assert backend.calls == []
    # DM-only plans do not need post pin detection.
    dm_plan = await cleanup.preview("1", [proposal(candidate(setup, "11", "DM"))])
    assert dm_plan["total_actions"] == 1


async def test_audit_append_only_and_decision_metadata(setup):
    cleanup, backend = setup
    decision = {"label": "DELETE", "engine": "JEV", "model_version": "v1", "reason": "external reason",
        "probabilities": {"KEEP": .02, "DELETE": .96, "REVIEW": .02}}
    plan = await cleanup.preview("1", [proposal(candidate(setup), decision)])
    approve(cleanup, plan)
    await cleanup.execute("1", plan["plan_id"], "audit-metadata-key", False)
    audit = cleanup.audit_history("1", limit=1)
    assert audit["items"][0]["decision"] == decision
    second = cleanup.audit_history("1", after=audit["next_cursor"])
    assert len(second["items"]) == 1 and second["items"][0]["result"] == "succeeded"
    for query in ["UPDATE audit SET result='failed'", "DELETE FROM audit"]:
        with pytest.raises(sqlite3.IntegrityError):
            with cleanup.repo.db:
                cleanup.repo.db.execute(query)


def test_current_post_contract_and_context_normalization():
    r = {"id": "10", "author_id": "1", "text": "short text", "note_post": {"text": "full text"},
         "referenced_posts": [{"type": "quoted", "id": "9"}], "attachments": {"media_keys": ["m1"]}}
    includes = {"posts": [{"id": "9", "text": "parent"}], "users": [{"id": "1", "username": "user"}],
                "media": [{"media_key": "m1", "type": "photo"}]}
    c = normalize("1", r, includes)
    assert c["content_type"] == "QUOTE" and c["text"] == "full text" and c["quote_id"] == "9"
    assert c["author"]["username"] == "user" and c["related_posts"][0]["text"] == "parent"
    assert c["media"][0]["type"] == "photo"


async def test_official_oauth_foreign_identity_is_not_stored(setup):
    cleanup, backend = setup
    def handler(request):
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "SECRET", "scope": "tweet.read users.read tweet.write"})
        return httpx.Response(200, json={"data": {"id": "2"}})
    def factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)
    oauth = XOAuth(cleanup.repo, factory)
    oauth.save_app("app")
    from urllib.parse import parse_qs, urlsplit
    state = parse_qs(urlsplit(oauth.begin("1")).query)["state"][0]
    with pytest.raises(Problem, match="exact X account"):
        await oauth.complete(state, "code")
    assert cleanup.repo.db.execute("SELECT COUNT(*) FROM credentials WHERE account='1'").fetchone()[0] == 0


async def test_official_archive_scan_uses_only_verified_account_and_cursor():
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path == "/2/users/me":
            return httpx.Response(200, json={"data": {"id": "1", "username": "my_account"}})
        return httpx.Response(200, json={"data": []})
    def factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)
    backend = OfficialX("SECRET", {"tweet.read", "users.read"}, factory)
    await backend.scan("1", "archive_posts", "NEXT_PAGE", 100)
    assert requests[-1].url.path == "/2/tweets/search/all"
    assert requests[-1].url.params["query"] == "from:my_account -is:retweet"
    assert requests[-1].url.params["next_token"] == "NEXT_PAGE"
    with pytest.raises(Problem, match="verified handle"):
        await backend.scan("2", "archive_posts")


async def test_archive_scan_limits_and_collection_bound_cursor(setup):
    cleanup, backend = setup
    backend.posts["10"] = raw()
    backend.next_token = "archive-next"
    result = await cleanup.scan("1", "archive_posts", limit=10)
    assert "entitlement" in result["history_limit"] and not result["complete_account_history"]
    with pytest.raises(Problem):
        await cleanup.scan("1", "posts", result["next_cursor"])
    with pytest.raises(Problem):
        await cleanup.scan("1", "archive_posts", limit=5)
