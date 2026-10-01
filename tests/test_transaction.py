from pathlib import Path
import httpx
import pytest
from bs4 import BeautifulSoup
from x_publisher.transaction import CurrentTransaction, asset_url, legacy_asset, transaction_import
from x_publisher.core import Problem


def test_legacy_and_current_module_resolution():
    assert legacy_asset('{"ondemand.s":"abc123"}')=='https://abs.twimg.com/responsive-web/client-web/ondemand.s.abc123a.js'
    assert legacy_asset('{12:"ondemand.s",34:"x"};{12:"abcd"}')=='https://abs.twimg.com/responsive-web/client-web/ondemand.s.abcda.js'
    assert transaction_import('import{R as te,t as e}from"./assets/code.js";S(te(function(){return K.options.context.featureSwitches.isTrue(`rweb_client_transaction_id_enabled`)}))')=='./assets/code.js'
    for bad in ['https://evil.example/sign.js','https://abs.twimg.com.evil.example/sign.js','https://user@abs.twimg.com/x-web/x-web/sign.js','//127.0.0.1/sign.js','https://abs.twimg.com/x-web/x-web/sign.js?q=secret']:
        with pytest.raises(Problem):asset_url('https://abs.twimg.com/x-web/x-web/entry.js',bad)


async def test_vite_parser_cookie_free_bounded_fetch(monkeypatch):
    original=httpx.AsyncClient
    seen=[]
    entry='https://abs.twimg.com/x-web/x-web/entry-client-logged-out-test.js'
    mapping={entry:'import{R as te}from"./assets/code.js";te(function(){return features.isTrue(`rweb_client_transaction_id_enabled`)})',
      'https://abs.twimg.com/x-web/x-web/assets/code.js':'import(`./sign.o-test.js`)',
      'https://abs.twimg.com/x-web/x-web/assets/sign.o-test.js':'f(e[22],16);f(e[30],16);f(e[35],16);f(e[6],16)'}
    async def handler(request):
        seen.append(str(request.url))
        assert 'cookie' not in request.headers and 'authorization' not in request.headers
        return httpx.Response(200,text=mapping[str(request.url)])
    monkeypatch.setattr('x_publisher.transaction.httpx.AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    soup=BeautifulSoup('<script src="'+entry+'"></script>','lxml')
    async with original(cookies={'auth_token':'never-send-to-cdn'}) as session:
        assert await CurrentTransaction().get_indices(soup,session,{'User-Agent':'test'})==(22,[30,35,6])
    assert len(seen)==3


async def test_bad_public_script_layout_fails_closed(monkeypatch):
    original=httpx.AsyncClient
    monkeypatch.setattr('x_publisher.transaction.httpx.AsyncClient',lambda **kw:original(transport=httpx.MockTransport(lambda req:httpx.Response(200,text='unexpected layout')),**kw))
    with pytest.raises(Problem):
        await CurrentTransaction().get_indices(BeautifulSoup('<script>m={"ondemand.s":"abc"}</script>','lxml'),None,{})


def test_native_browser_animation_regression():
    # Captured from X's public module in a cookie-free browser fixture, 50ms.
    assert CurrentTransaction().animate([102,51,196,179,170,186,230,69,96,69,31],50/4096) == '6532c41011eb851eb851ec011eb851eb851ec100'


def test_animation_time_rounding_matches_js(monkeypatch):
    t=CurrentTransaction()
    t.DEFAULT_ROW_INDEX=0
    t.DEFAULT_KEY_BYTES_INDICES=[1,2,3]
    monkeypatch.setattr(t,'get_2d_array',lambda *_:[[0]*11]*16)
    monkeypatch.setattr(t,'animate',lambda frame,target:target)
    assert t.get_animation_key([0,2,2,12],None)==50/4096  # 48ms -> 50ms
    assert t.get_animation_key([0,1,1,5],None)==10/4096   # Math.round(0.5), not banker's rounding
