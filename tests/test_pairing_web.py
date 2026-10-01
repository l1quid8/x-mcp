import httpx
import pytest
from x_publisher.app import create_app
from x_publisher.core import ORIGIN, SCOPES
from x_publisher.pairing_web import BASE
from test_publisher import store
from test_pairing import SessionBackend

async def test_https_browser_approval_and_one_time_claim(store):
    app = create_app(store, "owner-test-key", SessionBackend)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN) as c:
        started = (await c.post(BASE+'/start',json={'account':'premium','tier':'premium'})).json()
        headers={'Authorization':'Device '+started['device_code']}
        assert (await c.post(BASE+'/poll',headers=headers)).status_code == 202
        page = await c.get(started['verification_uri'])
        assert page.status_code == 200 and started['user_code'] in page.text
        assert page.headers["referrer-policy"] == "strict-origin"
        assert 'owner-test-key' not in page.text and started['device_code'] not in page.text
        from urllib.parse import parse_qs, urlsplit
        request_id=parse_qs(urlsplit(started['verification_uri']).query)['request'][0]
        body={'request':request_id,'key':'owner-test-key','match':'yes'}
        assert (await c.post(BASE+'/approve',data=body,headers={'Origin':'https://evil.example'})).status_code==403
        assert (await c.post(BASE+'/approve',data=body,headers={'Origin':'null'})).status_code==403
        assert (await c.post(BASE+'/approve',data=body,headers={'Origin':ORIGIN})).status_code==200
        grant=await c.post(BASE+'/poll',headers=headers)
        assert grant.status_code==200 and grant.json()['expected_user']=='premium'
        assert (await c.post(BASE+'/poll',headers=headers)).status_code==401
        assert (await c.post(BASE+'/approve',data=body,headers={'Origin':ORIGIN})).status_code==403
        # Public approval does not itself replace an account or expand publishing grants.
        assert store.session('1')['auth_token']=='secret-one'

async def test_wrong_owner_csrf_expiry_and_other_credentials(store):
    app = create_app(store, "owner-test-key", SessionBackend)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN) as c:
        started=(await c.post(BASE+'/start',json={'account':'premium','tier':'premium'})).json()
        from urllib.parse import parse_qs, urlsplit
        request_id=parse_qs(urlsplit(started['verification_uri']).query)['request'][0]
        body={'request':request_id,'key':'owner-test-key','match':'yes'}
        assert (await c.post(BASE+'/approve',data=body,headers={'Origin':ORIGIN})).status_code==403
        await c.get(started['verification_uri'])
        assert (await c.post(BASE+'/approve',data={**body,'key':'wrong'},headers={'Origin':ORIGIN})).status_code==403
        token=store.issue_token('test',SCOPES,['1'])
        for value in ['Bearer '+token, 'Device '+token, 'Device '+'x'*64]:
            assert (await c.post(BASE+'/poll',headers={'Authorization':value})).status_code==401
        with app.state.oauth.db:
            app.state.oauth.db.execute("UPDATE records SET expires=0 WHERE kind LIKE 'pair-%'")
        assert (await c.post(BASE+'/poll',headers={'Authorization':'Device '+started['device_code']})).status_code==401
        assert (await c.get(started['verification_uri'])).status_code==400

async def test_start_limits_and_bad_inputs(store):
    app=create_app(store,'owner-test-key',SessionBackend)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN) as c:
        assert (await c.post(BASE+'/start',json={'account':'premium','tier':'premium'},headers={'Origin':'https://evil.example'})).status_code==403
        assert (await c.post(BASE+'/start',json={'account':'bad/name','tier':'premium'})).status_code==400
        assert (await c.post(BASE+'/start',content=b' '*8193)).status_code==400
        for _ in range(8):
            assert (await c.post(BASE+'/start',json={'account':'premium','tier':'premium'})).status_code==200
        assert (await c.post(BASE+'/start',json={'account':'premium','tier':'premium'})).status_code==429

async def test_extension_origin_preflight_and_cookie_import(store):
    from x_publisher.extension_origin import EXTENSION_ORIGIN
    from x_publisher.pairing import issue_pairing, ENDPOINT
    app=create_app(store,'owner-test-key',SessionBackend)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN) as c:
        for path in [BASE+'/start',BASE+'/poll','/x-mcp/session-import']:
            headers={'Origin':EXTENSION_ORIGIN,'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'content-type, authorization'}
            result=await c.options(path,headers=headers)
            assert result.status_code==204
            assert result.headers['access-control-allow-origin']==EXTENSION_ORIGIN
            assert 'access-control-allow-credentials' not in result.headers
            for origin in ['https://evil.example','chrome-extension://'+'a'*32]:
                assert (await c.options(path,headers={**headers,'Origin':origin})).status_code==403
        started=await c.post(BASE+'/start',headers={'Origin':EXTENSION_ORIGIN},json={'account':'premium','tier':'premium'})
        assert started.status_code==200
        assert started.headers['access-control-allow-origin']==EXTENSION_ORIGIN
        assert (await c.post(BASE+'/approve',headers={'Origin':EXTENSION_ORIGIN},data={})).status_code==403
        grant=issue_pairing(store,'premium','premium')
        headers={'Origin':EXTENSION_ORIGIN,'Authorization':'Pairing '+grant['pairing_token']}
        result=await c.post(ENDPOINT,headers=headers,json={'cookies':{'auth_token':'extension-test','ct0':'csrf'}})
        assert result.status_code==200
        assert store.session('1')['auth_token']=='extension-test'
        assert result.headers['access-control-allow-origin']==EXTENSION_ORIGIN
        assert (await c.post(ENDPOINT,headers=headers,json={'cookies':{}})).status_code==401

async def test_multiple_approval_tabs_and_precise_key_errors(store):
    from urllib.parse import parse_qs, urlsplit
    app=create_app(store,'owner-test-key',SessionBackend)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN) as c:
        requests=[]
        for account in ['premium','free']:
            started=(await c.post(BASE+'/start',json={'account':account,'tier':'free'})).json()
            await c.get(started['verification_uri'])
            requests.append(parse_qs(urlsplit(started['verification_uri']).query)['request'][0])
        for public in requests:
            response=await c.post(BASE+'/approve',headers={'Origin':ORIGIN},data={'request':public,'key':' owner-test-key\n','match':'yes'})
            assert response.status_code==200
        replay=await c.post(BASE+'/approve',headers={'Origin':ORIGIN},data={'request':requests[0],'key':'owner-test-key','match':'yes'})
        assert replay.json()['error']=='approval_session_expired'
        started=(await c.post(BASE+'/start',json={'account':'premium','tier':'premium'})).json()
        await c.get(started['verification_uri'])
        public=parse_qs(urlsplit(started['verification_uri']).query)['request'][0]
        response=await c.post(BASE+'/approve',headers={'Origin':ORIGIN},data={'request':public,'key':'wrong','match':'yes'})
        assert response.status_code==403 and response.json()['error']=='owner_key_mismatch'
