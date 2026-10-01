"""Instance pool with failover, health scoring and freshness gating.

Public Nitter instances break in five ways, and each is handled here:

  down (DNS, refused, 502)          circuit breaker, fail over
  bot-walled (200 + Anubis HTML)    validate by parsing, not status codes
  rate-limited (429)                long cooldown, honours Retry-After
  capability gaps                   tracked per capability
  stale (200 + valid RSS, weeks old)  canary freshness gating

The last one is the awkward case, since the response looks entirely healthy.
A canary is fetched periodically and instances are judged on how far behind
they are, both against a ceiling and against the best mirror available.

Capability support and freshness are both tracked per capability rather than
per host, because instances break in parts: privacyredirect serves 22-day-old
timelines alongside 2-minute-old search results. Writing off the whole host
would cost us a working search mirror, and those are scarce.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import random
import socket
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable
from urllib.parse import urljoin, urlsplit

import httpx

from . import config
from .parse import ParseError, Post, newest_age_minutes, parse_feed

log = logging.getLogger("nitter_mcp.pool")

Capability = str  # "search" | "timeline"
CAPABILITIES: tuple[str, ...] = ("timeline", "search")
MAX_REDIRECTS = 3
REDIRECT_CODES = frozenset((301, 302, 303, 307, 308))


# Nitter's "feed switched off" page, which arrives with a 403 rather than a 200.
_DISABLED_MARKERS = ("rss feed is disabled", "rss is disabled", "feed is disabled")


def _feed_disabled(resp: httpx.Response) -> bool:
    if resp.status_code not in (403, 404, 451):
        return False
    return any(m in resp.text[:4000].lower() for m in _DISABLED_MARKERS)


class AllInstancesFailed(RuntimeError):
    def __init__(self, attempts: list[tuple[str, str]], capability: str = ""):
        self.attempts = attempts
        self.capability = capability
        detail = "; ".join(f"{host}: {err}" for host, err in attempts) or "no instances available"
        super().__init__(f"every Nitter instance failed for {capability or 'request'} -- {detail}")


@dataclass
class InstanceState:
    base_url: str
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    ewma_latency: float = 2.0
    cooldown_until: float = 0.0
    last_ok: float = 0.0
    last_error: str | None = None
    # capability -> does this instance serve it at all
    caps: dict[str, bool] = field(default_factory=dict)
    # capability -> age in minutes of the freshest item seen (freshness signal)
    canary_age: dict[str, float] = field(default_factory=dict)
    # capability -> proven to be serving outdated content
    stale: dict[str, bool] = field(default_factory=dict)
    # capability -> consecutive "looks dead" observations. Not persisted.
    # Working search mirrors are scarce, so a capability is only written off
    # after CAP_STRIKES confirmations rather than one flaky empty response.
    cap_strikes: dict[str, int] = field(default_factory=dict)
    last_checked: float = 0.0

    def strike(self, capability: Capability) -> bool:
        """Note a dead-looking response. Returns True once it's written off."""
        n = self.cap_strikes.get(capability, 0) + 1
        self.cap_strikes[capability] = n
        return n >= config.CAP_STRIKES

    @property
    def host(self) -> str:
        # This value is returned in tools and errors. Never echo credentials,
        # path components, or query strings from operator-supplied URLs.
        try:
            return urlsplit(self.base_url).hostname or "invalid-mirror"
        except ValueError:
            return "invalid-mirror"

    def available(self, now: float | None = None) -> bool:
        return (now or time.time()) >= self.cooldown_until

    def is_stale(self, capability: Capability) -> bool:
        return bool(self.stale.get(capability, False))

    def score(self, capability: Capability) -> float:
        """Higher is better. Drives instance ordering for one capability."""
        if self.is_stale(capability):
            return -50.0
        total = self.successes + self.failures
        reliability = (self.successes + 1) / (total + 2)  # Laplace-smoothed
        s = reliability * 10.0 - self.ewma_latency
        if self.caps.get(capability) is True:
            s += 6.0
        elif self.caps.get(capability) is False:
            s -= 25.0  # known not to serve this capability
        age = self.canary_age.get(capability)
        if age is not None:
            s -= min(age / 60.0, 5.0)  # prefer fresher mirrors
        return s

    def to_json(self) -> dict:
        return {
            "base_url": self.base_url,
            "successes": self.successes,
            "failures": self.failures,
            "ewma_latency": round(self.ewma_latency, 3),
            "caps": self.caps,
            "canary_age": self.canary_age,
            "stale": self.stale,
            "last_ok": self.last_ok,
            "last_error": self.last_error,
        }

    @classmethod
    def from_json(cls, d: dict) -> "InstanceState":
        st = cls(base_url=d["base_url"])
        st.successes = int(d.get("successes", 0))
        st.failures = int(d.get("failures", 0))
        st.ewma_latency = float(d.get("ewma_latency", 2.0))
        st.caps = {k: bool(v) for k, v in (d.get("caps") or {}).items()}
        st.last_ok = float(d.get("last_ok", 0.0))
        st.last_error = d.get("last_error")

        # Tolerate state written by the older per-instance schema.
        raw_age = d.get("canary_age")
        if isinstance(raw_age, dict):
            st.canary_age = {k: float(v) for k, v in raw_age.items() if v is not None}
        elif (legacy := d.get("canary_age_min")) is not None:
            st.canary_age = {"timeline": float(legacy)}

        raw_stale = d.get("stale")
        if isinstance(raw_stale, dict):
            st.stale = {k: bool(v) for k, v in raw_stale.items()}
        elif raw_stale is not None:
            st.stale = {"timeline": bool(raw_stale)}
        return st


class _TTLCache:
    def __init__(self, ttl: float, max_entries: int):
        self.ttl = ttl
        self.max_entries = max_entries
        self._data: dict[str, tuple[float, object]] = {}

    def get(self, key: str):
        hit = self._data.get(key)
        if not hit:
            return None
        expires, value = hit
        if time.time() > expires:
            self._data.pop(key, None)
            return None
        return value

    def put(self, key: str, value: object) -> None:
        if len(self._data) >= self.max_entries:
            for k in sorted(self._data, key=lambda k: self._data[k][0])[: self.max_entries // 4 or 1]:
                self._data.pop(k, None)
        self._data[key] = (time.time() + self.ttl, value)

    def clear(self) -> None:
        self._data.clear()


class NitterPool:
    def __init__(self, instances: Iterable[str] | None = None):
        self.instances: dict[str, InstanceState] = {
            url.rstrip("/"): InstanceState(base_url=url.rstrip("/"))
            for url in (instances or config.INSTANCES)
        }
        self._load_state()
        self._cache = _TTLCache(config.CACHE_TTL, config.CACHE_MAX_ENTRIES)
        self._sem = asyncio.Semaphore(config.MAX_CONCURRENCY)
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._last_request: dict[str, float] = {}
        self._client: httpx.AsyncClient | None = None
        self._last_health: float = 0.0
        self._health_lock = asyncio.Lock()

    # --- lifecycle ---------------------------------------------------------

    async def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={
                    "User-Agent": config.USER_AGENT,
                    "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                timeout=httpx.Timeout(config.REQUEST_TIMEOUT),
                follow_redirects=False,
                trust_env=False,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._save_state()

    # --- persistence -------------------------------------------------------

    def _load_state(self) -> None:
        path = config.state_path()
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            return
        for entry in raw.get("instances", []):
            url = entry.get("base_url", "").rstrip("/")
            if url in self.instances:
                try:
                    self.instances[url] = InstanceState.from_json(entry)
                except (KeyError, TypeError, ValueError):
                    continue  # corrupt entry: keep the fresh default
        self._last_health = float(raw.get("last_health", 0.0))

    def _save_state(self) -> None:
        path = config.state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "last_health": self._last_health,
                "instances": [s.to_json() for s in self.instances.values()],
            }
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2))
            tmp.replace(path)
        except OSError as exc:
            log.debug("could not persist state: %s", exc)

    # --- low-level fetch ---------------------------------------------------

    async def _throttle(self, host: str) -> None:
        """Keep at least MIN_INTERVAL_PER_HOST between hits on one instance."""
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            last = self._last_request.get(host, 0.0)
            wait = config.MIN_INTERVAL_PER_HOST - (time.time() - last)
            if wait > 0:
                await asyncio.sleep(wait + random.uniform(0, 0.15))
            self._last_request[host] = time.time()

    def _record_success(self, st: InstanceState, capability: Capability, latency: float) -> None:
        st.successes += 1
        st.consecutive_failures = 0
        st.cooldown_until = 0.0
        st.ewma_latency = 0.7 * st.ewma_latency + 0.3 * latency
        st.last_ok = time.time()
        st.last_error = None
        st.caps[capability] = True
        st.cap_strikes[capability] = 0

    def _record_failure(
        self,
        st: InstanceState,
        capability: Capability,
        err: str,
        *,
        cap_dead: bool = False,
        cooldown_override: float | None = None,
        definitive: bool = False,
    ) -> None:
        st.failures += 1
        st.last_error = err
        if cap_dead:
            # Instance is alive but appears not to serve this capability. Demote
            # only after repeated confirmation, and never trip the breaker for
            # the instance's other capabilities.
            if definitive or st.strike(capability):
                st.caps[capability] = False
            return
        st.consecutive_failures += 1
        backoff = cooldown_override if cooldown_override is not None else min(
            config.COOLDOWN_BASE * (2 ** (st.consecutive_failures - 1)),
            config.COOLDOWN_MAX,
        )
        st.cooldown_until = time.time() + backoff * random.uniform(0.8, 1.2)

    def _ordered(
        self,
        capability: Capability,
        *,
        include_stale: bool = False,
        spread: int | None = None,
    ) -> list[InstanceState]:
        now = time.time()

        # Tiered candidates. A cooling-down instance is a poor choice but still
        # beats returning nothing, so it goes in the tail instead of being
        # dropped. Without this a run of failures shrinks the list below
        # MAX_INSTANCE_ATTEMPTS and we give up with mirrors left untried.
        def serves(s: InstanceState) -> bool:
            return s.caps.get(capability) is not False

        def fresh(s: InstanceState) -> bool:
            return include_stale or not s.is_stale(capability)

        tier1 = [s for s in self.instances.values() if serves(s) and fresh(s) and s.available(now)]
        tier2 = [s for s in self.instances.values() if serves(s) and fresh(s) and not s.available(now)]
        # Last resort: capability believed dead, worth rechecking now and then
        # since they come back. Instances known to be stale for this capability
        # are never included at any tier; returning nothing beats returning
        # weeks-old content.
        tier3 = [s for s in self.instances.values() if not serves(s) and fresh(s)]

        by_score = lambda group: sorted(group, key=lambda s: s.score(capability), reverse=True)
        ranked = by_score(tier1) + by_score(tier2) + by_score(tier3)

        if spread is not None and len(ranked) > 1:
            # Fan-out mode: rotate the head of the list so N concurrent requests
            # land on N different hosts. Per-host throttling would otherwise
            # serialise them all behind the single best instance.
            healthy = [s for s in ranked if not s.is_stale(capability)] or ranked
            offset = spread % len(healthy)
            rotated = healthy[offset:] + healthy[:offset]
            tail = [s for s in ranked if s not in healthy]
            return rotated + tail
        return ranked

    async def _public_https_host(self, url: str) -> str:
        """Reject redirects and configured mirrors that target private services."""
        try:
            target = urlsplit(url)
            host = target.hostname
            port = target.port or 443
        except ValueError as exc:
            raise _InstanceError("invalid mirror URL") from exc
        if target.scheme.lower() != "https" or not host or target.username or target.password:
            raise _InstanceError("mirror target must be public HTTPS without userinfo")
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            try:
                resolved = await asyncio.to_thread(
                    socket.getaddrinfo, host, port, type=socket.SOCK_STREAM
                )
                addresses = [ipaddress.ip_address(row[4][0]) for row in resolved]
            except (OSError, UnicodeError, ValueError) as exc:
                raise _InstanceError("mirror DNS validation failed") from exc
        if not addresses or any(not address.is_global for address in addresses):
            raise _InstanceError("mirror target is not a public internet address")
        return host

    async def _get(self, st: InstanceState, url: str) -> httpx.Response:
        """One HTTP GET, retried once for transient failures.

        Transient covers dropped connections, timeouts and 502/503/504. These
        mirrors are small and blip constantly under load, so retrying here is
        cheaper than failing over to a worse instance.
        """
        client = await self.client()
        last_exc: Exception | None = None
        resp: httpx.Response | None = None
        for attempt in range(config.RETRIES_PER_INSTANCE + 1):
            try:
                current = url
                redirects = 0
                while True:
                    host = await self._public_https_host(current)
                    await self._throttle(host)
                    async with self._sem:
                        resp = await client.get(current, follow_redirects=False)
                    if resp.status_code not in REDIRECT_CODES:
                        break
                    location = resp.headers.get("Location")
                    if not location or redirects >= MAX_REDIRECTS:
                        raise _InstanceError("mirror redirect missing or limit exceeded")
                    current = urljoin(current, location)
                    redirects += 1
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                resp = None
            else:
                if resp.status_code not in (502, 503, 504):
                    return resp
            if attempt < config.RETRIES_PER_INSTANCE:
                await asyncio.sleep(0.4 * (attempt + 1) + random.uniform(0, 0.3))
        if resp is not None:
            return resp
        assert last_exc is not None
        raise last_exc

    async def _fetch_one(
        self, st: InstanceState, path: str, capability: Capability, *, canary: bool = False
    ) -> tuple[list[Post], str]:
        url = f"{st.base_url}{path}"
        t0 = time.perf_counter()
        resp = await self._get(st, url)
        latency = time.perf_counter() - t0

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            try:
                wait = float(retry_after) if retry_after else config.RATE_LIMIT_COOLDOWN
            except ValueError:
                wait = config.RATE_LIMIT_COOLDOWN
            raise _InstanceError("HTTP 429 rate limited", cooldown=max(wait, config.RATE_LIMIT_COOLDOWN))

        if resp.status_code != 200:
            # Some operators switch off one feed and leave the rest running.
            # nitter.net answers /search/rss with 403 "RSS feed is disabled"
            # while its timeline RSS stays fine, so this must retire the
            # capability rather than take the whole host offline.
            if _feed_disabled(resp):
                raise _InstanceError(
                    f"HTTP {resp.status_code}: RSS disabled for {capability} on this instance",
                    cap_dead=True,
                    definitive=True,
                )
            raise _InstanceError(f"HTTP {resp.status_code}")

        # A 200 proves nothing: bot walls and empty bodies both return 200.
        try:
            posts, feed_title = parse_feed(resp.text, instance=st.host)
        except ParseError as exc:
            # Empty body from a live host means the capability isn't served.
            # nitter.net also answers /search/rss this way, depending on which
            # layer of its stack replies first.
            raise _InstanceError(f"bad body: {exc}", cap_dead=not resp.text.strip()) from exc

        # An empty account feed may be legitimate, but an empty busy canary is
        # not evidence of a working capability. Do not reset its strike count.
        if posts or not canary:
            self._record_success(st, capability, latency)
        return posts, feed_title

    async def fetch(
        self,
        path: str,
        capability: Capability,
        *,
        require_items: bool = True,
        max_age_minutes: float | None = None,
        validate: Callable[[list[Post]], str | None] | None = None,
        use_cache: bool = True,
        spread: int | None = None,
    ) -> tuple[list[Post], str, str]:
        """Fetch `path` from the healthiest instance that can serve it.

        Returns (posts, feed_title, instance_host). Falls through the pool on any
        failure, including semantically-bad-but-HTTP-200 responses.
        """
        cache_key = f"{capability}:{path}:{max_age_minutes}"
        if use_cache:
            if (hit := self._cache.get(cache_key)) is not None:
                posts, title, host = hit  # type: ignore[misc]
                return list(posts), title, host

        await self.ensure_health()

        attempts: list[tuple[str, str]] = []
        empty_but_valid: tuple[list[Post], str, str] | None = None

        for st in self._ordered(capability, spread=spread)[: config.MAX_INSTANCE_ATTEMPTS]:
            try:
                posts, title = await self._fetch_one(st, path, capability)
            except _InstanceError as exc:
                self._record_failure(
                    st, capability, str(exc), cap_dead=exc.cap_dead,
                    cooldown_override=exc.cooldown, definitive=exc.definitive,
                )
                attempts.append((st.host, str(exc)))
                continue
            except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                self._record_failure(st, capability, f"{type(exc).__name__}: {exc}")
                attempts.append((st.host, f"{type(exc).__name__}"))
                continue

            if not posts:
                # Legitimately empty (silent account, no search hits) vs. broken
                # is ambiguous, so remember it but prefer an instance with data.
                empty_but_valid = ([], title, st.host)
                if require_items:
                    attempts.append((st.host, "no items"))
                    continue

            if max_age_minutes is not None and posts:
                age = newest_age_minutes(posts)
                if age is not None and age > max_age_minutes:
                    st.canary_age[capability] = age
                    attempts.append((st.host, f"stale ({age:.0f}m old)"))
                    continue

            if validate and (why := validate(posts)):
                attempts.append((st.host, f"rejected: {why}"))
                continue

            if use_cache:
                self._cache.put(cache_key, (posts, title, st.host))
            return posts, title, st.host

        if empty_but_valid is not None:
            return empty_but_valid
        if not attempts:
            # Nothing was even tried: every candidate was filtered out. Say why,
            # so the caller doesn't read this as "no such account".
            excluded = [s.host for s in self.instances.values() if s.is_stale(capability)]
            if excluded:
                attempts = [(h, f"excluded: serving stale {capability} content") for h in excluded]
        raise AllInstancesFailed(attempts, capability)

    # --- health / freshness ------------------------------------------------

    async def ensure_health(self, force: bool = False) -> None:
        if not force and (time.time() - self._last_health) < config.HEALTH_INTERVAL:
            return
        async with self._health_lock:
            if not force and (time.time() - self._last_health) < config.HEALTH_INTERVAL:
                return
            await self._health_sweep()

    async def _probe_capability(self, st: InstanceState, capability: Capability, path: str) -> None:
        """Measure liveness AND freshness for one capability on one instance."""
        try:
            posts, _ = await self._fetch_one(st, path, capability, canary=True)
        except _InstanceError as exc:
            self._record_failure(
                st, capability, str(exc), cap_dead=exc.cap_dead,
                cooldown_override=exc.cooldown, definitive=exc.definitive,
            )
            st.canary_age.pop(capability, None)
            return
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            self._record_failure(st, capability, f"{type(exc).__name__}: {exc}")
            st.canary_age.pop(capability, None)
            return
        finally:
            st.last_checked = time.time()

        if not posts:
            # A canary that returns nothing is probably not serving this
            # capability (nitter.net's search returns a valid-but-empty feed),
            # but confirm before condemning it.
            self._record_failure(st, capability, "empty canary feed", cap_dead=True)
            st.canary_age.pop(capability, None)
            return
        age = newest_age_minutes(posts)
        if age is not None:
            st.canary_age[capability] = age

    async def _health_sweep(self) -> None:
        probes = {
            "timeline": f"/{config.CANARY_ACCOUNT}/rss",
            "search": f"/search/rss?f=tweets&q={config.CANARY_SEARCH}",
        }
        # Probe every capability on every instance: capabilities fail
        # independently, so a host that is useless for one may be the best
        # available for another.
        await asyncio.gather(
            *(
                self._probe_capability(st, cap, path)
                for st in self.instances.values()
                for cap, path in probes.items()
            ),
            return_exceptions=True,
        )

        # Staleness verdict is computed per capability, against the best mirror
        # for *that* capability.
        for cap in CAPABILITIES:
            ages = [s.canary_age[cap] for s in self.instances.values() if cap in s.canary_age]
            best = min(ages) if ages else None
            for st in self.instances.values():
                age = st.canary_age.get(cap)
                if age is None:
                    st.stale.pop(cap, None)  # unknown, not proven stale
                    continue
                too_old = age > config.MAX_CANARY_AGE_MIN
                lagging = best is not None and (age - best) > config.MAX_CANARY_LAG_MIN
                st.stale[cap] = bool(too_old or lagging)
                if st.stale[cap]:
                    log.info(
                        "instance %s marked stale for %s (canary %.0fm, best %.0fm)",
                        st.host, cap, age, best if best is not None else -1,
                    )

        self._last_health = time.time()
        self._save_state()

    # --- introspection -----------------------------------------------------

    def snapshot(self) -> dict:
        now = time.time()
        rows = []
        for st in sorted(self.instances.values(), key=lambda s: s.score("search"), reverse=True):
            rows.append({
                "instance": st.host,
                "available": st.available(now),
                "capabilities": {
                    cap: {
                        "supported": st.caps.get(cap),
                        "stale": st.is_stale(cap),
                        "canary_age_minutes": st.canary_age.get(cap),
                        "usable": bool(
                            st.available(now)
                            and st.caps.get(cap) is not False
                            and not st.is_stale(cap)
                        ),
                    }
                    for cap in CAPABILITIES
                },
                "successes": st.successes,
                "failures": st.failures,
                "avg_latency_s": round(st.ewma_latency, 2),
                "cooldown_remaining_s": max(0, round(st.cooldown_until - now)) if not st.available(now) else 0,
                "last_error": st.last_error,
            })

        usable = {
            cap: sum(1 for r in rows if r["capabilities"][cap]["usable"])
            for cap in CAPABILITIES
        }
        return {
            "instances": rows,
            "usable_by_capability": usable,
            "last_health_check_age_s": round(now - self._last_health) if self._last_health else None,
            "canary_account": config.CANARY_ACCOUNT,
            "canary_search": config.CANARY_SEARCH,
        }

    def clear_cache(self) -> None:
        self._cache.clear()


class _InstanceError(Exception):
    def __init__(
        self,
        msg: str,
        cap_dead: bool = False,
        cooldown: float | None = None,
        definitive: bool = False,
    ):
        super().__init__(msg)
        self.cap_dead = cap_dead
        self.cooldown = cooldown
        # The server said outright that this feed is off, so no confirmation
        # round is needed before believing it.
        self.definitive = definitive
