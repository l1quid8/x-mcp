"""Resolve X's current public transaction module without executing downloaded code.

Twikit's pinned implementation expects a pre-March-2026 webpack map. Support
that map, its numeric variant (upstream PR 410), and the observed x-web modules.
Fail closed if the upstream structure changes. No synthetic transaction IDs.
"""
import re
import math
from urllib.parse import urljoin, urlsplit
import httpx
from twikit.x_client_transaction import ClientTransaction
from .core import Problem

INDICES = re.compile(r'\[(\d{1,3})\],\s*16')
IMPORTS = re.compile(r'''import\{([^}]+)\}from["']([^"']+)["']''')


def asset_url(base, reference):
    url = urljoin(base, reference)
    parts = urlsplit(url)
    if (parts.scheme != 'https' or parts.netloc != 'abs.twimg.com' or parts.query or parts.fragment
        or not re.fullmatch(r'/(?:x-web/x-web/|responsive-web/client-web/)[A-Za-z0-9_./-]+\.js', parts.path)):
        raise Problem('backend_error', 'X transaction module location is unsupported')
    return url


def legacy_asset(source):
    old = re.search(r'''["']ondemand\.s["']\s*:\s*["']([a-zA-Z0-9_]+)["']''', source)
    if old:
        return 'https://abs.twimg.com/responsive-web/client-web/ondemand.s.'+old[1]+'a.js'
    index = re.search(r'''(?:[,\{])(\d+):["']ondemand\.s["']''', source)
    if index:
        digest = re.search(r'''(?:[,\{])'''+index[1]+r''':["']([a-zA-Z0-9_]+)["']''', source)
        if digest:
            return 'https://abs.twimg.com/responsive-web/client-web/ondemand.s.'+digest[1]+'a.js'
    return None


def transaction_import(source):
    call = re.search(r'([\w$]+)\(function\(\)\{return[^}]{0,300}rweb_client_transaction_id_enabled', source)
    if call:
        for spec, module in IMPORTS.findall(source):
            if any(re.search(r'\bas\s+'+re.escape(call[1])+r'\s*$', item) for item in spec.split(',')):
                return module
    raise Problem('backend_error', 'X transaction module could not be located in the current entry script')


class CurrentTransaction(ClientTransaction):
    def get_animation_key(self, key_bytes, response):
        # X's current browser module quantizes Web Animation currentTime to the
        # nearest 10 ms. The pinned Twikit port omits that quantization.
        row = key_bytes[self.DEFAULT_ROW_INDEX] % 16
        frame_time = math.prod(key_bytes[index] % 16 for index in self.DEFAULT_KEY_BYTES_INDICES)
        frame_time = math.floor(frame_time / 10 + 0.5) * 10
        frames = self.get_2d_array(key_bytes, response)
        return self.animate(frames[row], frame_time / 4096)

    async def get_indices(self, home_page_response, session, headers):
        source = str(home_page_response)
        url = legacy_asset(source)
        # CDN requests use a separate cookie-free client: imported X sessions
        # must never be forwarded to static assets, redirects, or arbitrary hosts.
        async with httpx.AsyncClient(timeout=httpx.Timeout(25, connect=10), follow_redirects=False, trust_env=False) as public:
            async def fetch(url):
                checked = asset_url(url, url)
                async with public.stream('GET', checked, headers={'User-Agent': headers.get('User-Agent','')}) as response:
                    if response.status_code != 200:
                        raise Problem('backend_error', 'X public transaction asset is unavailable')
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data)+len(chunk)>2*1024*1024:
                            raise Problem('backend_error', 'X public transaction asset exceeds the supported size')
                        data.extend(chunk)
                return data.decode('utf-8')
            if url is None:
                entries = [n.get('src','') for n in home_page_response.select('script[src]')
                           if re.fullmatch(r'https://abs\.twimg\.com/x-web/x-web/entry-client-[A-Za-z0-9_-]+\.js', n.get('src',''))]
                if len(entries) != 1:
                    raise Problem('backend_error', 'Unsupported X page format during transaction initialization')
                entry = entries[0]
                entry_source = await fetch(entry)
                module = asset_url(entry, transaction_import(entry_source))
                module_source = await fetch(module)
                sign = re.search(r'''import\([`"'](\./sign\.o-[A-Za-z0-9_-]+\.js)[`"']\)''', module_source)
                if not sign:
                    raise Problem('backend_error', 'X signing module could not be located')
                url = asset_url(module, sign[1])
            script = await fetch(url)
        indices = [int(value) for value in INDICES.findall(script)]
        if len(indices)!=4 or any(value>=48 for value in indices):
            raise Problem('backend_error', 'X transaction index layout is unsupported')
        return indices[0], indices[1:]

    async def init(self, session, headers):
        try:
            await super().init(session, headers)
        except Exception:
            # Twikit treats any assigned home_page_response as initialized.
            # Reset partial state so a failed parse can never masquerade as ready.
            self.home_page_response = None
            raise
