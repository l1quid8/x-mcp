from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from x_publisher.backend import XBackend
from x_publisher.core import Problem


def backend(response):
    b = XBackend.__new__(XBackend)
    b.client = SimpleNamespace(gql=SimpleNamespace(create_tweet=AsyncMock(return_value=(response,None)),tweet_detail=AsyncMock(return_value=(response,None))))
    return b

@pytest.mark.parametrize('long_post', [False,True])
async def test_receipt_without_optional_user_fields(long_post):
    branch='notetweet_create' if long_post else 'create_tweet'
    b=backend({'data':{branch:{'tweet_results':{'result':{'__typename':'Tweet','rest_id':'321'}}}}})
    result=await b.create_post({'text':'test','long_post':long_post}, [], None, None)
    assert result == {'id':'321','url':'https://x.com/i/status/321'}
    assert b.client.gql.create_tweet.await_count == 1

async def test_unrecognized_submission_is_unknown_without_retry():
    b=backend({'data':{}})
    with pytest.raises(Problem) as e:
        await b.create_post({'text':'test','long_post':False}, [], None, None)
    assert e.value.code=='unknown_outcome'
    assert b.client.gql.create_tweet.await_count==1

@pytest.mark.parametrize('pid,author,expected', [('321','123',True),('999','123',False),('321','999',False)])
async def test_verification_checks_both_ids(pid,author,expected):
    b=backend({'entries':[{'content':{'result':{'__typename':'Tweet','rest_id':pid,'legacy':{'user_id_str':author}}}}]})
    assert await b.verify_post('321','123') is expected

async def test_conflicting_author_rejected():
    b=backend({'__typename':'Tweet','rest_id':'321','legacy':{'user_id_str':'123'},'core':{'user_results':{'result':{'rest_id':'999'}}}})
    assert await b.verify_post('321','123') is False
