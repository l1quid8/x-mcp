"""Cleanup through the owner's existing encrypted publisher session."""
from datetime import datetime, timezone
import time

import httpx

from .backend import XBackend
from .core import Problem
from .x_api import POST_SCOPES, XAPIError, rate_headers

# Verified against X's public web bundle main.d0bb33e09c6a2565a.js on
# 2026-09-29. Twikit's bundled persisted mutation uses an older operation ID.
DELETE_TWEET_QUERY_ID = 'nxpZCY2K-I6QoFHAHeojFQ'
DELETE_TWEET_ENDPOINT = 'https://x.com/i/api/graphql/'+DELETE_TWEET_QUERY_ID+'/DeleteTweet'
DELETE_RETWEET_QUERY_ID = 'ZyZigVsNiFO6v1dEks1eWg'
DELETE_RETWEET_ENDPOINT = 'https://x.com/i/api/graphql/'+DELETE_RETWEET_QUERY_ID+'/DeleteRetweet'


def unwrap(result):
    if isinstance(result, dict) and result.get('__typename') == 'TweetWithVisibilityResults':
        result = result.get('tweet')
    return result


def post(result):
    result = unwrap(result)
    if not isinstance(result, dict) or not isinstance(result.get('legacy'), dict):
        raise XAPIError('invalid_x_response')
    legacy = result['legacy']
    user = result.get('core', {}).get('user_results', {}).get('result', {})
    aid = user.get('rest_id') or legacy.get('user_id_str')
    pid = result.get('rest_id')
    if not isinstance(pid, str) or not pid.isdigit() or not isinstance(aid, str) or not aid.isdigit():
        raise XAPIError('invalid_x_response')
    refs = []
    if legacy.get('in_reply_to_status_id_str'):
        refs.append({'type': 'replied_to', 'id': legacy['in_reply_to_status_id_str']})
    if legacy.get('quoted_status_id_str'):
        refs.append({'type': 'quoted', 'id': legacy['quoted_status_id_str']})
    repost = unwrap(legacy.get('retweeted_status_result', {}).get('result'))
    if repost:
        refs.append({'type': 'retweeted', 'id': repost['rest_id']})
    text = (result.get('note_tweet', {}).get('note_tweet_results', {}).get('result', {}).get('text')
            or legacy.get('full_text'))
    created = legacy.get('created_at')
    if created:
        try:
            created = datetime.strptime(created, '%a %b %d %H:%M:%S %z %Y').astimezone(timezone.utc).isoformat()
        except (ValueError, TypeError):
            raise XAPIError('invalid_x_response') from None
    metrics = {dest: legacy[src] for src, dest in [('favorite_count', 'like_count'),
        ('reply_count', 'reply_count'), ('retweet_count', 'retweet_count'), ('quote_count', 'quote_count'),
        ('bookmark_count', 'bookmark_count')] if isinstance(legacy.get(src), int)}
    media = legacy.get('extended_entities', {}).get('media', [])
    return {'id': pid, 'author_id': aid, 'text': text, 'created_at': created,
        'conversation_id': legacy.get('conversation_id_str'), 'referenced_tweets': refs,
        'public_metrics': metrics, 'attachments': {'media_keys': [str(m['id_str']) for m in media if m.get('id_str')]}}, {
        'users': [{'id': aid, 'username': (user.get('core') or {}).get('screen_name') or (user.get('legacy') or {}).get('screen_name')}],
        'media': [{'media_key': str(m['id_str']), 'type': m.get('type'), 'url': m.get('media_url_https'),
                   'alt_text': m.get('ext_alt_text')} for m in media if m.get('id_str')]}


def instructions(response):
    user = response.get('data', {}).get('user', {}).get('result', {})
    for name in ('timeline_v2', 'timeline'):
        timeline = user.get(name, {})
        found = timeline.get('timeline', timeline).get('instructions')
        if isinstance(found, list):
            return found
    raise XAPIError('invalid_x_response')


def entry_posts(entry):
    content = entry.get('content', {})
    if content.get('promotedMetadata'):
        return []
    item = content.get('itemContent', {})
    if item.get('promotedMetadata'):
        return []
    result = item.get('tweet_results', {}).get('result')
    if result:
        return [result]
    results = []
    for child in content.get('items', []):
        inner = child.get('item', {}).get('itemContent', {})
        if not inner.get('promotedMetadata') and inner.get('tweet_results', {}).get('result'):
            results.append(inner['tweet_results']['result'])
    return results


class SessionCleanup:
    source = 'x_session'
    history_limit = 'profile_timeline_visibility; no_complete_history_guarantee'

    def __init__(self, store, account, backend_factory=XBackend):
        self.account = account
        self.backend = backend_factory(store.session(account))
        self.backend.client.http.timeout = httpx.Timeout(20, connect=10)
        self.client = self.backend.client
        self.rate = {}
        self.request_rates = {}
        self._verified_identity = None
        self._identity_verified_at = 0.0
        self._pinned_cache = None
        self._pinned_cached_at = 0.0
        self.username = store.account(account)['username']

    async def close(self):
        await self.backend.close()

    def require(self, scopes):
        if not set(scopes) <= POST_SCOPES:
            raise Problem('session_cleanup_unsupported', 'Session cleanup supports posts, replies, quotes and reposts; DM cleanup is unavailable')

    def require_action(self, action):
        if action not in {'DELETE_POST', 'UNDO_REPOST'}:
            raise Problem('session_cleanup_unsupported', 'Live session cleanup supports authored posts, replies, quotes and repost undo')

    def request_error(self, exc, mutation=False):
        if isinstance(exc, Problem) and not mutation:
            return exc
        name = type(exc).__name__
        self.rate = rate_headers(getattr(exc, 'headers', None) or {})
        if name == 'TooManyRequests':
            return XAPIError('rate_limited', 429, max(time.time()+60, self.rate.get('reset', 0)), self.rate)
        if name in {'Unauthorized', 'Forbidden', 'AccountLocked', 'AccountSuspended'}:
            return XAPIError('session_or_account_restricted', 403)
        if name == 'NotFound':
            return XAPIError('x_rejected' if mutation else 'x_not_found', 404)
        if name == 'BadRequest':
            return XAPIError('x_rejected', 400)
        if isinstance(exc, httpx.HTTPError) or name in {'ServerError', 'RequestTimeout'}:
            return XAPIError('transport_unknown' if mutation else 'transport_error')
        return XAPIError('remote_unknown' if mutation else 'invalid_x_response')

    def remember_rate(self, operation, rate):
        if rate:
            self.request_rates[operation] = dict(rate)

    async def call(self, fn, *args, mutation=False):
        try:
            result, response = await fn(*args)
        except Exception as exc:
            error = self.request_error(exc, mutation)
            self.remember_rate(fn.__name__, self.rate)
            raise error from None
        self.rate = rate_headers(response.headers)
        self.remember_rate(fn.__name__, self.rate)
        if not isinstance(result, dict) or result.get('errors'):
            raise XAPIError('remote_unknown' if mutation else 'incomplete_x_response', response.status_code)
        if not 200 <= response.status_code < 300:
            raise XAPIError('remote_unknown' if mutation else 'x_rejected', response.status_code)
        return result

    async def identity(self):
        # Verification makes X requests before the timeline request. Classify
        # those failures too, so scans preserve rate limits and session errors.
        try:
            identity = await self.backend.identity()
        except Exception as exc:
            error = self.request_error(exc)
            operation = {'authenticated_settings': 'authenticated_settings',
                         'identity_lookup': 'user_by_screen_name'}.get(getattr(self.backend, 'verification_stage', ''), 'identity')
            self.remember_rate(operation, self.rate)
            raise error from None
        finally:
            for operation, rate in getattr(self.backend, 'identity_rates', {}).items():
                self.remember_rate(operation, rate)
        if identity.get('username'):
            self.username = identity['username']
        self._verified_identity = dict(identity)
        self._identity_verified_at = time.monotonic()
        return identity

    async def dispatch_identity(self, account):
        # Cleanup verifies this fixed-cookie backend when the bounded run starts.
        # Reuse that result only within the same short run; direct adapter calls
        # and older sessions still perform a fresh authenticated identity check.
        if account != self.account:
            raise Problem('identity_mismatch', 'Session belongs to a different account')
        if (self._verified_identity is None
                or time.monotonic() - self._identity_verified_at > 45):
            identity = await self.identity()
        else:
            identity = self._verified_identity
        if identity.get('id') != account:
            raise Problem('identity_mismatch', 'Session belongs to a different account')
        return identity

    async def pinned(self, account):
        if account != self.account:
            raise Problem('identity_mismatch', 'Session belongs to a different account')
        if self._pinned_cached_at and time.monotonic() - self._pinned_cached_at <= 30:
            return self._pinned_cache
        result = await self.call(self.client.gql.user_by_screen_name, self.username)
        user = result.get('data', {}).get('user', {}).get('result', {})
        legacy = user.get('legacy')
        if user.get('rest_id') != account or not isinstance(legacy, dict) or 'pinned_tweet_ids_str' not in legacy:
            raise XAPIError('pinned_state_unavailable')
        pins = legacy['pinned_tweet_ids_str']
        if not isinstance(pins, list) or len(pins) > 1 or any(not isinstance(p, str) or not p.isdigit() for p in pins):
            raise XAPIError('pinned_state_unavailable')
        self._pinned_cache = pins[0] if pins else None
        self._pinned_cached_at = time.monotonic()
        return self._pinned_cache

    async def scan(self, account, kind, cursor=None, limit=100):
        if kind not in {'posts', 'replies'}:
            raise Problem('session_cleanup_unsupported', 'Use collection=posts or replies with the connected X session; full-archive and DM scans are unavailable')
        if account != self.account:
            raise Problem('identity_mismatch', 'Session belongs to a different account')
        fetch = self.client.gql.user_tweets if kind == 'posts' else self.client.gql.user_tweets_and_replies
        response = await self.call(fetch, account, limit, cursor)
        data, includes, seen, next_cursor = [], {'users': [], 'media': []}, set(), None
        for instruction in instructions(response):
            entries = instruction.get('entries', [])
            if instruction.get('entry'):
                entries = [instruction['entry'], *entries]
            for entry in entries:
                content = entry.get('content', {})
                if content.get('cursorType') == 'Bottom':
                    next_cursor = content.get('value')
                for result in entry_posts(entry):
                    result = unwrap(result)
                    if result.get('__typename') in {'TweetTombstone', 'TweetUnavailable'}:
                        continue
                    raw, inc = post(result)
                    if raw['id'] not in seen:
                        data.append(raw)
                        seen.add(raw['id'])
                        includes['users'].extend(inc['users'])
                        includes['media'].extend(inc['media'])
        # Pinned entries are not chronological. Present newest authored items first.
        data.sort(key=lambda p: int(p['id']), reverse=True)
        return {'data': data, 'includes': includes, 'meta': {'next_token': next_cursor if next_cursor != cursor else None}}

    async def lookup(self, candidate):
        if candidate['content_type'] == 'DM':
            raise Problem('session_cleanup_unsupported', 'DM cleanup is unavailable through the connected session')
        response = await self.call(self.client.gql.tweet_result_by_rest_id, candidate['content_id'])
        container = response.get('data', {}).get('tweetResult')
        if not isinstance(container, dict):
            raise XAPIError('invalid_x_response')
        result = unwrap(container.get('result'))
        if not result or result.get('__typename') in {'TweetTombstone', 'TweetUnavailable'}:
            raise XAPIError('x_not_found', 404)
        raw, includes = post(result)
        return {'data': raw, 'includes': includes}

    async def delete_tweet(self, target):
        return await self.client.gql.gql_post(DELETE_TWEET_ENDPOINT,
            {'tweet_id': target, 'dark_request': False})

    async def delete_retweet(self, source_target):
        return await self.client.gql.gql_post(DELETE_RETWEET_ENDPOINT,
            {'source_tweet_id': source_target})

    async def execute_repost(self, account, source_target, repost_id):
        await self.dispatch_identity(account)
        fresh = await self.lookup({'content_type': 'POST', 'content_id': repost_id})
        if fresh['data']['author_id'] != account:
            raise Problem('not_owned', 'The authenticated account does not own this repost')
        refs = fresh['data'].get('referenced_tweets', [])
        if not any(r.get('type') == 'retweeted' and r.get('id') == source_target for r in refs):
            raise Problem('target_mismatch', 'The repost source changed since preview')
        response = await self.call(self.delete_retweet, source_target, mutation=True)
        acknowledgment = response.get('data', {}).get('unretweet', {})
        if not isinstance(acknowledgment, dict) or not isinstance(acknowledgment.get('source_tweet_results'), dict):
            raise XAPIError('remote_unknown', 200)
        try:
            await self.lookup({'content_type': 'POST', 'content_id': repost_id})
        except XAPIError as exc:
            if exc.code == 'x_not_found':
                return {'http_status': 200, 'backend': 'x_session', 'target_id': repost_id,
                        'source_target_id': source_target, 'data': {'undone': True},
                        'confirmed_by': 'undo_acknowledgment_and_readback',
                        'operation_query_id': DELETE_RETWEET_QUERY_ID, 'rate_limit': self.rate}
        raise XAPIError('remote_unknown', 200)

    async def execute(self, account, action, target):
        self.require_action(action)
        if action == 'UNDO_REPOST':
            raise Problem('session_cleanup_internal', 'Repost undo requires its frozen repost target')
        await self.dispatch_identity(account)
        # Repeat ownership immediately before dispatch, even for direct adapter callers.
        fresh = await self.lookup({'content_type': 'POST', 'content_id': target})
        if fresh['data']['author_id'] != account:
            raise Problem('not_owned', 'The authenticated account does not own this post')
        if any(r['type'] == 'retweeted' for r in fresh['data']['referenced_tweets']):
            raise Problem('session_cleanup_unsupported', 'Use a repost-specific adapter; authored-post deletion cannot undo a repost')
        response = await self.call(self.delete_tweet, target, mutation=True)
        acknowledgment = response.get('data', {}).get('delete_tweet')
        if not isinstance(acknowledgment, dict) or not isinstance(acknowledgment.get('tweet_results'), dict):
            raise XAPIError('remote_unknown', 200)
        try:
            await self.lookup({'content_type': 'POST', 'content_id': target})
        except XAPIError as exc:
            if exc.code == 'x_not_found':
                return {'http_status': 200, 'backend': 'x_session', 'target_id': target,
                        'data': {'deleted': True}, 'confirmed_by': 'delete_acknowledgment_and_readback',
                        'operation_query_id': DELETE_TWEET_QUERY_ID, 'rate_limit': self.rate}
        # A read failure or surviving post cannot establish success, and must never replay.
        raise XAPIError('remote_unknown', 200)

    async def execute_browser_verified(self, account, action, target):
        """Dispatch an exact locally reviewed browser target; never fabricate read-back success.

        Only Cleanup's sealed plan/approval path calls this method after validating
        a short-lived local browser-observation attestation. No MCP input exposes it.
        """
        self.require_action(action)
        await self.dispatch_identity(account)
        response = await self.call(self.delete_tweet, target, mutation=True)
        acknowledgement = response.get('data', {}).get('delete_tweet')
        if not isinstance(acknowledgement, dict) or not isinstance(acknowledgement.get('tweet_results'), dict):
            raise XAPIError('remote_unknown', 200)
        return {'http_status': 200, 'backend': 'x_session', 'target_id': target,
                'data': {'delete_acknowledged': True}, 'confirmed_by': 'delete_acknowledgment_only',
                'requires_owner_verification': True, 'operation_query_id': DELETE_TWEET_QUERY_ID,
                'rate_limit': self.rate}
