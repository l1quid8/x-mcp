// The source template is inert. Generate an extension for your own HTTPS origin.
export const ORIGIN = 'https://mcp.example.invalid';
export const BASE = `${ORIGIN}/x-mcp/oauth/pair`;
export const IMPORT = `${ORIGIN}/x-mcp/session-import`;
export const NAMES = ['auth_token', 'ct0', 'twid', 'kdt', 'att', 'lang'];

export function validateStart(data) {
  const url = new URL(data.verification_uri);
  if (url.origin !== ORIGIN || url.pathname !== '/x-mcp/oauth/pair/approve' || url.hash || url.username || url.password ||
      [...url.searchParams.keys()].join(',') !== 'request' || !/^[A-Za-z0-9_-]{43}$/.test(url.searchParams.get('request')) ||
      !/^[A-Za-z0-9_-]{64}$/.test(data.device_code) || !/^[A-Z2-9]{4}-[A-Z2-9]{4}$/.test(data.user_code)) {
    throw new Error('Unexpected approval page. No session was sent.');
  }
  return data;
}
export function validateGrant(data, account) {
  if (data.endpoint !== IMPORT || data.expected_user !== account || !/^[A-Za-z0-9_-]{64}$/.test(data.pairing_token)) {
    throw new Error('Unexpected account grant. No session was sent.');
  }
  return data;
}
export function filterCookies(items) {
  const result = {};
  for (const item of items) {
    if (item && item.domain.replace(/^\./, '') === 'x.com' && NAMES.includes(item.name) && item.value) {
      result[item.name] = item.value;
    }
  }
  if (!result.auth_token || !result.ct0) throw new Error('No signed-in X session found in this browser profile. Open X and sign in first.');
  return result;
}
export const messages = {
  account_mismatch: 'The active X account does not match your selection. Switch accounts on X, then connect again. Nothing was replaced.',
  invalid_pairing: 'The approval expired or was already used. Start a new connection.',
  expired_or_used: 'The approval expired or was already used. Start a new connection.',
  rate_limited: 'A rate limit was reached. Wait before connecting again.',
  account_busy: 'This account is publishing. Reconnect when the operation finishes.',
  session_or_account_restricted: 'X rejected verification of this session. Check for a challenge in X. Browser login alone does not guarantee server acceptance.',
  backend_error: 'X MCP could not verify this X session. The publishing backend needs investigation.',
  import_busy: 'Another account connection is in progress. Try again shortly.'
};
