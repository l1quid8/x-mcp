"""Identity verification must not depend on optional public profile fields."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import pytest
from x_publisher.backend import XBackend
from x_publisher.core import Problem


def backend(settings, result):
    b = XBackend.__new__(XBackend)
    b.client = SimpleNamespace(v11=SimpleNamespace(settings=AsyncMock(return_value=(settings, None))),
        gql=SimpleNamespace(user_by_screen_name=AsyncMock(return_value=({'data': {'user': {'result': result}}}, None))))
    return b


@pytest.mark.parametrize('section', ['legacy', 'core'])
async def test_identity_without_optional_profile_fields(section):
    b = backend({'screen_name': 'OwnedAccount'}, {'__typename': 'User', 'rest_id': '123', section: {'screen_name': 'ownedaccount'}})
    assert await b.identity() == {'id': '123', 'username': 'ownedaccount'}
    b.client.gql.user_by_screen_name.assert_awaited_once_with('OwnedAccount')
    assert b.client._user_id == '123'


@pytest.mark.parametrize('settings', [{}, {'screen_name': None}, {'screen_name': '../other'}, []])
async def test_missing_authenticated_identity_never_uses_public_lookup(settings):
    b = backend(settings, {})
    with pytest.raises(Problem):
        await b.identity()
    b.client.gql.user_by_screen_name.assert_not_awaited()


@pytest.mark.parametrize('result', [
    {}, {'__typename': 'UserUnavailable'},
    {'__typename': 'User', 'rest_id': '123', 'legacy': {'screen_name': 'other'}},
    {'__typename': 'User', 'rest_id': 'bad-id', 'legacy': {'screen_name': 'owned'}},
    {'__typename': 'User', 'rest_id': '123', 'legacy': None},
])
async def test_inconsistent_lookup_is_rejected(result):
    b = backend({'screen_name': 'owned'}, result)
    with pytest.raises(Problem):
        await b.identity()


async def test_expired_session_stops_before_lookup():
    b = backend({}, {})
    b.client.v11.settings.side_effect = RuntimeError('expired')
    with pytest.raises(RuntimeError):
        await b.identity()
    b.client.gql.user_by_screen_name.assert_not_awaited()


async def test_identity_preserves_distinct_numeric_quotas_without_headers():
    b = backend({'screen_name': 'owned'}, {'__typename': 'User', 'rest_id': '123', 'core': {'screen_name': 'owned'}})
    b.client.v11.settings.return_value = ({'screen_name': 'owned'}, httpx.Response(200, headers={
        'x-rate-limit-limit': '75', 'x-rate-limit-remaining': '60', 'x-rate-limit-reset': '9999999999',
        'set-cookie': 'SECRET'}))
    result = b.client.gql.user_by_screen_name.return_value[0]
    b.client.gql.user_by_screen_name.return_value = (result, httpx.Response(200, headers={
        'x-rate-limit-limit': '150', 'x-rate-limit-remaining': '140', 'x-rate-limit-reset': '9999999998',
        'authorization': 'SECRET'}))
    assert await b.identity() == {'id': '123', 'username': 'owned'}
    assert b.identity_rates == {
        'authenticated_settings': {'limit': 75, 'remaining': 60, 'reset': 9999999999},
        'user_by_screen_name': {'limit': 150, 'remaining': 140, 'reset': 9999999998}}
    assert 'SECRET' not in str(b.identity_rates)
