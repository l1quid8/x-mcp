"""Tunables and the seeded instance pool.

Everything is overridable by environment variable, since the public Nitter
fleet churns and retuning shouldn't need a code change.
"""

from __future__ import annotations

import os
from pathlib import Path

# --- helpers ---------------------------------------------------------------


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_list(name: str) -> list[str] | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    return [p.strip().rstrip("/") for p in raw.split(",") if p.strip()]


# --- instance pool ---------------------------------------------------------

# From a sweep of the public fleet on 2026-08-07. This is only a starting
# order: instances are re-scored at runtime, so a wrong seed corrects itself
# within a health cycle. The annotations are why the ordering looks odd.
# Instances differ in what they actually serve, not just whether they are up.
DEFAULT_INSTANCES: list[str] = [
    # timeline fresh, search fresh. The only all-round healthy mirror found.
    "https://nitter.perennialte.ch",
    # timeline fresh, search off. Its /search/rss returns 403 "RSS feed is
    # disabled" (or a bare empty 200, depending which layer answers first).
    # The web UI search does work, but only over HTML to a real browser, so
    # there is nothing here for an RSS client to use.
    "https://nitter.net",
    # timeline ~22d stale, search fresh. Kept for search only.
    "https://nitter.privacyredirect.com",
    # round-robin redirector, reaches mirrors not listed here. Any single
    # request may land on a bad one, but it is a useful long tail.
    "https://farside.link/nitter",
    # currently bot-walled or down, kept as cheap probes in case they return
    "https://nitter.tiekoetter.com",
    "https://nitter.space",
    "https://nitter.poast.org",
    "https://nitter.catsarch.com",
]

INSTANCES: list[str] = _env_list("X_MCP_READER_INSTANCES") or DEFAULT_INSTANCES

# Public instances gate on User-Agent. A browser UA gets a Cloudflare/Anubis
# challenge or a 400 "only works inside an RSS client"; an RSS-reader UA passes.
USER_AGENT: str = os.environ.get(
    "X_MCP_READER_USER_AGENT",
    "Mozilla/5.0 (compatible; Miniflux/2.1.3; +https://miniflux.app)",
)

# --- timing / reliability --------------------------------------------------

REQUEST_TIMEOUT: float = _env_float("X_MCP_READER_TIMEOUT", 12.0)
CACHE_TTL: float = _env_float("X_MCP_READER_CACHE_TTL", 60.0)
CACHE_MAX_ENTRIES: int = _env_int("X_MCP_READER_CACHE_MAX_ENTRIES", 512)

# Circuit breaker: cooldown doubles per consecutive failure, capped.
COOLDOWN_BASE: float = _env_float("X_MCP_READER_COOLDOWN_BASE", 30.0)
COOLDOWN_MAX: float = _env_float("X_MCP_READER_COOLDOWN_MAX", 1800.0)

# A 429 means back off, not broken, so park the host for longer than a normal
# failure and honour Retry-After when one is sent.
RATE_LIMIT_COOLDOWN: float = _env_float("X_MCP_READER_RATE_LIMIT_COOLDOWN", 900.0)

# Politeness: these are free volunteer-run instances.
MIN_INTERVAL_PER_HOST: float = _env_float("X_MCP_READER_MIN_INTERVAL", 0.7)
MAX_CONCURRENCY: int = _env_int("X_MCP_READER_MAX_CONCURRENCY", 6)

# How many instances to try before giving up on a single logical request.
MAX_INSTANCE_ATTEMPTS: int = _env_int("X_MCP_READER_MAX_ATTEMPTS", 5)

# In-place retries per instance for transient failures (dropped connection,
# timeout, 502/503/504) before failing over to the next mirror.
RETRIES_PER_INSTANCE: int = _env_int("X_MCP_READER_RETRIES_PER_INSTANCE", 1)

# Consecutive "capability looks dead" observations before believing it. Guards
# scarce search-capable mirrors against a single flaky empty response.
CAP_STRIKES: int = _env_int("X_MCP_READER_CAP_STRIKES", 2)

# --- freshness gating ------------------------------------------------------

# A dead instance is easy to spot. A stale one returns HTTP 200 and valid RSS
# that happens to be weeks old, so we watch a busy account and treat the age of
# its newest post as a proxy for how far behind the instance is.
CANARY_ACCOUNT: str = os.environ.get("X_MCP_READER_CANARY", "Reuters")

# Search is probed separately because the two fail independently. Needs to be a
# term with constant global volume, so that "no results" is a real signal.
CANARY_SEARCH: str = os.environ.get("X_MCP_READER_CANARY_SEARCH", "news")

# Absolute ceiling: canary older than this => instance is stale.
MAX_CANARY_AGE_MIN: float = _env_float("X_MCP_READER_MAX_CANARY_AGE_MIN", 180.0)

# Relative gate: an instance lagging this far behind the *best* instance is
# stale even if it passes the absolute check.
MAX_CANARY_LAG_MIN: float = _env_float("X_MCP_READER_MAX_CANARY_LAG_MIN", 90.0)

# Re-run the health sweep at most this often.
HEALTH_INTERVAL: float = _env_float("X_MCP_READER_HEALTH_INTERVAL", 600.0)

# --- persistence -----------------------------------------------------------


def state_path() -> Path:
    override = os.environ.get("X_MCP_READER_STATE_DIR")
    base = Path(override) if override else Path.home() / ".cache" / "x-mcp" / "reader"
    return base / "state.json"


# --- curated newswire sources ---------------------------------------------

# get_breaking_news fans out over these rather than using keyword search, which
# returns mostly engagement-bait retweets. Going straight to the wires is much
# better signal for "what just happened".
TOPIC_SOURCES: dict[str, list[str]] = {
    "world": [
        "Reuters", "AP", "AFP", "BBCWorld", "BBCBreaking",
        "AJEnglish", "DeutscheWelle", "SkyNews", "ReutersWorld",
    ],
    "us": [
        "AP", "Reuters", "CNNBrk", "NBCNews", "CBSNews",
        "axios", "politico", "washingtonpost", "nytimes",
    ],
    "business": [
        "Reuters", "business", "WSJ", "FT", "CNBC",
        "WSJmarkets", "TheEconomist", "YahooFinance",
    ],
    "tech": [
        "TechCrunch", "verge", "WIRED", "arstechnica",
        "Reuters", "TheRegister", "engadget",
    ],
    "ai": [
        "OpenAI", "AnthropicAI", "GoogleDeepMind", "huggingface",
        "TechCrunch", "arstechnica", "rowancheung",
    ],
    "crypto": [
        "Cointelegraph", "CoinDesk", "BitcoinMagazine",
    ],
    "science": [
        "NASA", "sciencemagazine", "Nature", "NewScientist", "ScienceAlert",
    ],
    "sports": [
        "espn", "BBCSport", "SkySportsNews", "TheAthletic",
    ],
}

DEFAULT_TOPIC = "world"
