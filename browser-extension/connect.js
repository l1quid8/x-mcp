import {BASE, IMPORT, NAMES, validateStart, validateGrant, filterCookies, messages} from './protocol.mjs';
const el = id => document.getElementById(id);
let current = null;

async function request(url, body, authorization, controller, importing = false) {
  const timeout = setTimeout(() => controller.abort(), importing ? 110000 : 30000);
  try {
    const response = await fetch(url, {method: 'POST', credentials: 'omit', redirect: 'error', cache: 'no-store',
      referrerPolicy: 'no-referrer', signal: controller.signal,
      headers: {'Content-Type': 'application/json', ...(authorization ? {Authorization: authorization} : {})},
      body: JSON.stringify(body || {})});
    const data = await response.json();
    if (!response.ok) throw new Error(messages[data.error] || 'The server rejected this connection. No automatic retry was made.');
    return data;
  } catch (error) {
    if (controller.signal.aborted) throw new Error(importing ? 'Import interrupted. The session may already be saved; check the server account list before reconnecting.' : 'Connection canceled or timed out.');
    // Never render arbitrary network/server errors, which can contain URLs or credentials.
    if (Object.values(messages).includes(error.message) || error.message === 'The server rejected this connection. No automatic retry was made.') throw error;
    throw new Error(importing ? 'Import response was interrupted. Check the server account list before reconnecting.' : 'Could not reach X MCP over HTTPS.');
  } finally {clearTimeout(timeout);}
}
function pause(ms, signal) {
  return new Promise((resolve, reject) => {
    const abort = () => {clearTimeout(timer); reject(new Error('Connection canceled.'));};
    const timer = setTimeout(() => {signal.removeEventListener('abort', abort); resolve();}, ms);
    if (signal.aborted) {clearTimeout(timer); reject(new Error('Connection canceled.'));}
    else signal.addEventListener('abort', abort, {once: true});
  });
}
el('cancel').addEventListener('click', () => current?.abort());
el('connect').addEventListener('click', async () => {
  if (current) return;
  const account = el('account').value.trim().replace(/^@/, '').toLowerCase();
  if (!/^[a-z0-9_]{1,15}$/.test(account)) {
    el('status').textContent = 'Enter a valid X handle before connecting.';
    return;
  }
  const tier = el('tier').value;
  const controller = new AbortController();
  current = controller;
  el('connect').disabled = true;
  el('account').disabled = true;
  el('cancel').hidden = false;
  el('status').textContent = 'Requesting permission to connect the selected X account…';
  let cookies = null, grant = null, started = null;
  try {
    // Must be called directly from a user gesture. No automatic background collection.
    const allowed = await chrome.permissions.request({permissions: ['cookies'], origins: ['https://x.com/*']});
    if (!allowed) throw new Error('Permission was not granted. No session was read or sent.');
    if (controller.signal.aborted) throw new Error('Connection canceled.');
    started = validateStart(await request(`${BASE}/start`, {account, tier}, null, controller));
    el('code').textContent = started.user_code;
    el('approval').href = started.verification_uri;
    el('pair').hidden = false;
    el('status').textContent = 'Approve the matching code in the X MCP tab. Keep this tab open.';
    await chrome.tabs.create({url: started.verification_uri});
    const deadline = Date.now() + 600000;
    while (Date.now() < deadline) {
      await pause(2000, controller.signal);
      const result = await request(`${BASE}/poll`, {}, `Device ${started.device_code}`, controller);
      if (result.state === 'pending') continue;
      grant = validateGrant(result, account);
      break;
    }
    if (!grant) throw new Error('Approval expired. Start a new connection.');
    if (controller.signal.aborted) throw new Error('Connection canceled.');
    el('status').textContent = 'Verifying the selected X account and saving its session encrypted…';
    // Read only named cookies for X, only after owner approval, in this browser profile.
    cookies = filterCookies(await Promise.all(NAMES.map(name => chrome.cookies.get({url: 'https://x.com/', name}))));
    if (controller.signal.aborted) throw new Error('Connection canceled.');
    const result = await request(IMPORT, {cookies}, `Pairing ${grant.pairing_token}`, controller, true);
    if (!result.session_verified || !/^\d+$/.test(result.account_id) || !/^[A-Za-z0-9_]{1,15}$/.test(result.username) || result.username.toLowerCase() !== account) {
      throw new Error('Unexpected verification response. Check the server account list before reconnecting.');
    }
    el('status').textContent = `Connected @${result.username} (account ${result.account_id}). Session stored encrypted.\nNo posts were sent. MCP client account permissions are unchanged.`;
  } catch (error) {
    // Chrome API exceptions are deliberately not displayed verbatim.
    const safe = ['Permission was not granted. No session was read or sent.', 'Connection canceled.', 'Approval expired. Start a new connection.',
      'Unexpected approval page. No session was sent.', 'Unexpected account grant. No session was sent.',
      'No signed-in X session found in this browser profile. Open X and sign in first.',
      'Unexpected verification response. Check the server account list before reconnecting.',
      'The server rejected this connection. No automatic retry was made.', 'Connection canceled or timed out.',
      'Import interrupted. The session may already be saved; check the server account list before reconnecting.',
      'Import response was interrupted. Check the server account list before reconnecting.', 'Could not reach X MCP over HTTPS.', ...Object.values(messages)];
    el('status').textContent = safe.includes(error.message) ? error.message : 'Connection failed. Check extension permissions and X MCP availability.';
  } finally {
    if (cookies) for (const name of Object.keys(cookies)) delete cookies[name];
    cookies = grant = started = null;
    el('pair').hidden = true;
    el('approval').removeAttribute('href');
    el('code').textContent = '';
    el('connect').disabled = false;
    el('account').disabled = false;
    el('cancel').hidden = true;
    current = null;
  }
});
