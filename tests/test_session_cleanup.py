import copy
import hashlib
import time
from types import SimpleNamespace

import httpx
import pytest

from x_publisher.cleanup import Cleanup
from x_publisher.cleanup_models import Candidate, ProposedAction
from x_publisher.core import Problem, canonical
from x_publisher.session_cleanup import SessionCleanup, DELETE_TWEET_ENDPOINT
from x_publisher.x_api import XAPIError
from twikit.errors import Forbidden, TooManyRequests
from test_cleanup import setup


def tweet(pid, author='1', **legacy):
    return {'__typename': 'Tweet', 'rest_id': pid,
        'core': {'user_results': {'result': {'rest_id': author, 'core': {'screen_name': 'user'+author}}}},
        'legacy': {'created_at': 'Tue Sep 29 06:00:00 +0000 2026', 'full_text': 'test post',
                   'favorite_count': 0, **legacy}}


class Graph:
    def __init__(self):
        self.posts = {'100': tweet('100'), '200': tweet('200', in_reply_to_status_id_str='99')}
        self.calls = []
        self.delete_error = None
        self.survives = False
        self.pin_present = True
        self.mutations = []

    def response(self, data):
        return data, httpx.Response(200, json=data)

    async def user_tweets_and_replies(self, account, limit, cursor):
        entries = [{'entryId': 'tweet-'+p['rest_id'], 'content': {'itemContent': {'tweet_results': {'result': copy.deepcopy(p)}}}} for p in self.posts.values()]
        entries.append({'entryId': 'cursor-bottom', 'content': {'cursorType': 'Bottom', 'value': 'next-page'}})
        return self.response({'data': {'user': {'result': {'timeline': {'timeline': {'instructions': [{'entries': entries}]}}}}}})

    async def user_tweets(self, account, limit, cursor):
        return await self.user_tweets_and_replies(account, limit, cursor)

    async def user_by_rest_id(self, account):
        return self.response({'data': {'user': {'result': {'rest_id': account,
            'legacy': {'pinned_tweet_ids_str': []} if self.pin_present else {}}}}})

    async def user_by_screen_name(self, username):
        return await self.user_by_rest_id('1')

    async def tweet_result_by_rest_id(self, pid):
        return self.response({'data': {'tweetResult': {'result': copy.deepcopy(self.posts.get(pid))}}})

    async def delete_tweet(self, pid):
        self.calls.append(pid)
        if self.delete_error:
            raise self.delete_error
        if not self.survives:
            self.posts.pop(pid, None)
        return self.response({'data': {'delete_tweet': {'tweet_results': {'result': {}}}}})

    async def gql_post(self, endpoint, variables):
        self.mutations.append((endpoint, variables))
        assert endpoint == DELETE_TWEET_ENDPOINT
        assert variables['dark_request'] is False
        return await self.delete_tweet(variables['tweet_id'])


class Backend:
    def __init__(self, graph, identity='1'):
        self.client = SimpleNamespace(gql=graph, http=SimpleNamespace(timeout=None))
        self.identity_id = identity
        self.identity_calls = 0
        self.closed = False

    async def identity(self):
        self.identity_calls += 1
        return {'id': self.identity_id}

    async def close(self):
        self.closed = True


def adapter(store, graph, identity='1'):
    backend = Backend(graph, identity)
    return SessionCleanup(store, '1', lambda cookies: backend)


@pytest.mark.parametrize('verified', [True, False])
async def test_receipt_deletion_needs_no_scan_and_rejects_unverified_receipts(setup, verified):
    store = setup[0].store
    graph = Graph()
    payload = canonical({'posts': [{'text': 'test'}]})
    result = canonical({'posts': [{'id': '100', 'verified': verified}]})
    with store.db:
        store.db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?)',
            ('receipt-op', '1', 'receipt-key', 'hash', payload, 'succeeded', result, 'done', time.time(), time.time()))
    async def broken_scan(*args):
        raise AssertionError('receipt deletion must not scan')
    graph.user_tweets = broken_scan
    async def factory(account):
        return adapter(store, graph)
    cleanup = Cleanup(store, factory, live_enabled=True)
    if not verified:
        with pytest.raises(Problem, match='No verified publication receipt'):
            await cleanup.preview_post_deletion('1', '100')
        assert graph.calls == []
        return
    with pytest.raises(Problem, match='No verified publication receipt'):
        await cleanup.preview_post_deletion('1', '999')
    plan = await cleanup.preview_post_deletion('1', '100')
    assert plan['total_actions'] == 1 and graph.calls == []
    assert plan['items'][0]['candidate']['source'] == 'publication_receipt'
    result = await cleanup.execute('1', plan['plan_id'], 'receipt-live-key', False, 1, client_authorized=True)
    assert result['progress']['succeeded'] == 1
    assert graph.calls == ['100'] and '200' in graph.posts


async def test_session_scan_preserves_provenance_and_latest_order(setup):
    store = setup[0].store
    graph = Graph()
    created = []
    async def factory(account):
        b = adapter(store, graph)
        created.append(b)
        return b
    cleanup = Cleanup(store, factory, live_enabled=False)
    page = await cleanup.scan('1', limit=5)
    assert [c['content_id'] for c in page['candidates']] == ['200', '100']
    assert [c['content_type'] for c in page['candidates']] == ['REPLY', 'POST']
    assert all(c['source'] == 'x_session' and c['author_id'] == '1' for c in page['candidates'])
    assert created[0].backend.closed
    candidate = page['candidates'][0]
    plan = await cleanup.preview('1', [ProposedAction(candidate_id=candidate['candidate_id'], action='DELETE_POST')])
    assert plan['total_actions'] == 1
    dry = await cleanup.execute('1', plan['plan_id'], 'dry-test-key', max_actions=1)
    assert dry['remote_mutations'] == 0 and graph.calls == []
    assert all(b.backend.closed for b in created)


async def test_execution_preserves_profile_quota_alongside_post_readback_quota(setup):
    graph = Graph()
    def response(data):
        profile = 'user' in data.get('data', {})
        headers = {'x-rate-limit-limit': '150' if profile else '500',
                   'x-rate-limit-remaining': '140' if profile else '450',
                   'x-rate-limit-reset': '9999999999', 'set-cookie': 'SECRET'}
        return data, httpx.Response(200, headers=headers, json=data)
    graph.response = response
    async def factory(account):
        b = adapter(setup[0].store, graph)
        b.backend.identity_rates = {'authenticated_settings': {'limit': 75, 'remaining': 65, 'reset': 9999999999}}
        return b
    cleanup = Cleanup(setup[0].store, factory, live_enabled=True)
    scan = await cleanup.scan('1', limit=5)
    plan = await cleanup.preview('1', [ProposedAction(candidate_id=scan['candidates'][0]['candidate_id'], action='DELETE_POST')])
    with cleanup.repo.db:
        cleanup.repo.db.execute('INSERT INTO approvals VALUES (?,?,?,?)', (plan['plan_id'], plan['plan_digest'], time.time()+60, 1))
    result = await cleanup.execute('1', plan['plan_id'], 'quota-aware-live-key', dry_run=False, max_actions=1)
    assert result['progress']['succeeded'] == 1 and graph.calls == ['200']
    rates = result['request_rate_limits']
    assert rates['authenticated_settings']['remaining'] == 65
    assert rates['user_by_screen_name']['limit'] == 150
    assert rates['tweet_result_by_rest_id']['limit'] == 500
    assert 'SECRET' not in str(rates)


@pytest.mark.parametrize('collection', ['posts', 'replies'])
@pytest.mark.parametrize('failure,code,status', [
    (TooManyRequests('private session details', headers={'x-rate-limit-limit': '150',
        'x-rate-limit-remaining': '0', 'x-rate-limit-reset': '9999999999',
        'authorization': 'private credential'}), 'rate_limited', 429),
    (Forbidden('private session details'), 'session_or_account_restricted', 403),
    (httpx.ReadTimeout('private session details'), 'transport_error', 0),
])
async def test_scan_identity_failures_preserve_classification_and_close_session(setup, collection, failure, code, status):
    graph = Graph()
    b = adapter(setup[0].store, graph)
    async def failed_identity():
        raise failure
    b.backend.identity = failed_identity
    async def factory(account):
        return b
    cleanup = Cleanup(setup[0].store, factory)
    with pytest.raises(XAPIError) as result:
        await cleanup.scan('1', collection, limit=5)
    error = result.value
    assert error.code == code and error.status == status
    if code == 'rate_limited':
        assert error.retry_at == 9999999999
        assert error.rate == {'limit': 150, 'remaining': 0, 'reset': 9999999999}
    assert 'private' not in str(error)
    assert b.backend.closed and graph.calls == []
    assert cleanup.repo.db.execute('SELECT COUNT(*) FROM candidates').fetchone()[0] == 0


async def test_session_delete_verifies_ownership_and_readback(setup):
    store = setup[0].store
    graph = Graph()
    b = adapter(store, graph)
    receipt = await b.execute('1', 'DELETE_POST', '100')
    assert graph.calls == ['100'] and receipt['data']['deleted']
    graph.posts['300'] = tweet('300', author='2')
    with pytest.raises(Problem, match='does not own'):
        await b.execute('1', 'DELETE_POST', '300')
    assert graph.calls == ['100']


async def test_bounded_run_reuses_recent_fixed_session_identity(setup):
    graph = Graph()
    b = adapter(setup[0].store, graph)
    assert (await b.identity())['id'] == '1'
    await b.execute('1', 'DELETE_POST', '100')
    assert b.backend.identity_calls == 1
    assert graph.calls == ['100']


async def test_current_web_deletion_preserves_exact_long_id(setup):
    graph = Graph()
    target = '1700909578693583031'
    graph.posts[target] = tweet(target)
    b = adapter(setup[0].store, graph)
    await b.execute('1', 'DELETE_POST', target)
    assert graph.mutations == [(DELETE_TWEET_ENDPOINT, {'tweet_id': target, 'dark_request': False})]


@pytest.mark.parametrize('failure', ['timeout', 'surviving_post'])
async def test_session_uncertain_deletion_never_claims_success(setup, failure):
    store = setup[0].store
    graph = Graph()
    if failure == 'timeout':
        graph.delete_error = httpx.ReadTimeout('secret upstream data')
    else:
        graph.survives = True
    b = adapter(store, graph)
    with pytest.raises(XAPIError) as exc:
        await b.execute('1', 'DELETE_POST', '100')
    assert exc.value.code in {'remote_unknown', 'transport_unknown'}
    assert 'secret' not in str(exc.value)
    assert graph.calls == ['100']


async def test_session_missing_pins_and_unsupported_collections_fail_closed(setup):
    store = setup[0].store
    graph = Graph()
    graph.pin_present = False
    b = adapter(store, graph)
    with pytest.raises(XAPIError) as exc:
        await b.pinned('1')
    assert exc.value.code == 'pinned_state_unavailable'
    with pytest.raises(Problem):
        await b.scan('1', 'dms')
    with pytest.raises(Problem):
        await b.execute('1', 'UNDO_REPOST', '100')
    assert graph.calls == []


async def test_session_wrong_account_and_cleanup_unknown_ledger(setup):
    store = setup[0].store
    graph = Graph()
    async def wrong(account):
        return adapter(store, graph, identity='2')
    cleanup = Cleanup(store, wrong, live_enabled=True)
    with pytest.raises(Problem, match='different account'):
        await cleanup.scan('1', limit=5)
    async def factory(account):
        return adapter(store, graph)
    cleanup.backend_factory = factory
    page = await cleanup.scan('1', limit=5)
    plan = await cleanup.preview('1', [ProposedAction(candidate_id=page['candidates'][0]['candidate_id'], action='DELETE_POST')])
    with cleanup.repo.db:
        cleanup.repo.db.execute('INSERT INTO approvals VALUES (?,?,?,?)', (plan['plan_id'], plan['plan_digest'], 9999999999, 1))
    graph.delete_error = httpx.ReadTimeout('do not expose')
    result = await cleanup.execute('1', plan['plan_id'], 'live-test-key', dry_run=False, max_actions=1)
    assert result['progress']['unknown'] == 1
    await cleanup.execute('1', plan['plan_id'], 'live-test-key', dry_run=False, max_actions=1)
    assert len(graph.calls) == 1


@pytest.mark.parametrize('attested', [False, True])
@pytest.mark.parametrize('source', ['owner_browser_observation', 'public_x_embed'])
async def test_browser_observation_requires_local_review_and_never_fakes_success(setup, attested, source):
    store = setup[0].store
    graph = Graph()
    async def factory(account):
        return adapter(store, graph)
    cleanup = Cleanup(store, factory, live_enabled=True)
    c = Candidate(candidate_id='browser-observation-1', account_id='1', content_id='300',
        content_type='POST', author_id='1', text='owner screenshot', observed_at=time.time(),
        source=source).model_dump(mode='json')
    with cleanup.repo.db:
        cleanup.repo.db.execute('INSERT INTO candidates VALUES (?,?,?,?)', (c['candidate_id'],'1',canonical(c),time.time()))
        if attested:
            cleanup.repo.db.execute('INSERT INTO browser_observations VALUES (?,?,?,?,?)',
                (c['candidate_id'],'1',hashlib.sha256(canonical(c).encode()).hexdigest(),time.time()+300,'user-supplied screenshot'))
    plan = await cleanup.preview('1', [ProposedAction(candidate_id=c['candidate_id'],action='DELETE_POST')])
    if not attested:
        assert plan['total_actions'] == 0
        assert plan['validation_failures'][0]['code'] == 'browser_observation_unverified'
        return
    assert plan['total_actions'] == 1
    with cleanup.repo.db:
        cleanup.repo.db.execute('INSERT INTO approvals VALUES (?,?,?,?)', (plan['plan_id'],plan['plan_digest'],time.time()+300,1))
    result = await cleanup.execute('1', plan['plan_id'], 'browser-review-key', dry_run=False, max_actions=1)
    assert graph.calls == ['300']
    assert result['progress']['unknown'] == 1 and result['progress']['succeeded'] == 0
    assert result['items'][0]['receipt']['requires_owner_verification']
    await cleanup.execute('1', plan['plan_id'], 'browser-review-key', dry_run=False, max_actions=1)
    assert graph.calls == ['300']


async def test_browser_observation_expiry_and_protections(setup):
    store = setup[0].store
    graph = Graph()
    async def factory(account):
        return adapter(store, graph)
    cleanup = Cleanup(store, factory, live_enabled=True)
    c = Candidate(candidate_id='browser-observation-2',account_id='1',content_id='300',
        content_type='POST',author_id='1',observed_at=time.time(),source='owner_browser_observation').model_dump(mode='json')
    digest=hashlib.sha256(canonical(c).encode()).hexdigest()
    with cleanup.repo.db:
        cleanup.repo.db.execute('INSERT INTO candidates VALUES (?,?,?,?)',(c['candidate_id'],'1',canonical(c),time.time()))
        cleanup.repo.db.execute('INSERT INTO browser_observations VALUES (?,?,?,?,?)',(c['candidate_id'],'1',digest,time.time()+300,'screenshot'))
    from x_publisher.cleanup_models import ProtectionPolicy
    cleanup.repo.set_policy('1',ProtectionPolicy(protected_ids=['300']))
    plan = await cleanup.preview('1',[ProposedAction(candidate_id=c['candidate_id'],action='DELETE_POST')])
    assert plan['total_actions']==0 and plan['protected_or_skipped'][0]['code']=='protected_id'
    with cleanup.repo.db:
        cleanup.repo.db.execute('UPDATE browser_observations SET expires=0')
    with pytest.raises(Problem):cleanup.repo.verify_browser_observation('1',c)
    assert graph.calls==[]
