import assert from 'node:assert/strict';
import {test} from 'node:test';
import fs from 'node:fs';
import crypto from 'node:crypto';
import {filterCookies,validateStart,validateGrant,BASE,IMPORT,ORIGIN} from '../../browser-extension/protocol.mjs';

test('reads only named X cookies, requires authentication and csrf cookies',()=>{
  const cookies=[{name:'auth_token',value:'a',domain:'.x.com'},{name:'ct0',value:'c',domain:'x.com'},
    {name:'auth_token',value:'evil',domain:'x.com.evil.example'},{name:'other',value:'secret',domain:'.x.com'},null];
  assert.deepEqual(filterCookies(cookies),{auth_token:'a',ct0:'c'});
  assert.throws(()=>filterCookies([cookies[0]]));
});
test('fixed destination and expected account required before secret transfer',()=>{
  const start={verification_uri:BASE+'/approve?request='+'r'.repeat(43),device_code:'s'.repeat(64),user_code:'ABCD-2345'};
  assert.equal(validateStart(start),start);
  for(const uri of ['https://evil.example/approve',start.verification_uri+'&request=evil',start.verification_uri+'#secret']) {
    assert.throws(()=>validateStart({...start,verification_uri:uri}));
  }
  const grant={endpoint:IMPORT,expected_user:'example_user',pairing_token:'s'.repeat(64)};
  assert.equal(validateGrant(grant,'example_user'),grant);
  assert.throws(()=>validateGrant(grant,'other_user'));
  assert.throws(()=>validateGrant({...grant,endpoint:'https://evil.example'},'example_user'));
});
test('extension ID matches exact server origin; no broad hosts or content scripts',()=>{
  const m=JSON.parse(fs.readFileSync(new URL('../../browser-extension/manifest.json',import.meta.url)));
  const id=[...crypto.createHash('sha256').update(Buffer.from(m.key,'base64')).digest('hex').slice(0,32)].map(c=>String.fromCharCode(97+parseInt(c,16))).join('');
  const server=fs.readFileSync(new URL('../../src/x_publisher/extension_origin.py',import.meta.url),'utf8');
  assert.ok(server.includes('chrome-extension://'+id));
  assert.deepEqual(m.optional_permissions,['cookies']);
  assert.deepEqual(m.optional_host_permissions,['https://x.com/*']);
  assert.equal(ORIGIN,'https://mcp.example.invalid');
  assert.deepEqual(m.host_permissions,[ORIGIN+'/*']);
  assert.ok(m.content_security_policy.extension_pages.includes('connect-src '+ORIGIN+';'));
  assert.equal(m.content_scripts,undefined);
  assert.equal(m.externally_connectable,undefined);
});
