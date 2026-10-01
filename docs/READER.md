# Reader notes

These notes describe the Nitter-based reader inherited from
[Alastrantia/nitter-mcp](https://github.com/Alastrantia/nitter-mcp). X MCP now
exposes these reader tools through its one shared MCP server. The upstream code
and its MIT license are credited in [source provenance](PROVENANCE.md). The
instance observations below are historical; check `reader_status` for current
health.

Reading Nitter's RSS is the easy part. The hard part is that most public
instances are broken, and they don't tell you so. A dead instance is obvious; one
that returns HTTP 200 with a perfectly valid feed of three-week-old posts is not.
Most of this code exists to deal with that.

## Install

Use the [main X MCP instructions](../README.md). The old standalone reader
commands and connection paths do not apply to this combined service.

## Tools

### `get_breaking_news`

Usually the one you want. It merges the timelines of known newswire accounts
(Reuters, AP, AFP, BBC, Bloomberg and friends) and returns them newest first.
Nitter's keyword search is full of engagement-bait retweets, so for "what just
happened" questions, going straight to the wires is much better signal.

```
get_breaking_news(topic="world", query=None, limit=30, within_hours=12, sources=None)
```

Topics are `world`, `us`, `business`, `tech`, `ai`, `crypto`, `science`,
`sports`. `query` filters the merged feed by keyword, and `sources` replaces the
account list with your own. The fan-out is spread across instances so that
fetching 9 accounts doesn't queue up behind a single host.

### `search_x`

```
search_x(query="", limit=20, from_user=None, exclude_retweets=True,
         exclude_replies=False, lang=None, since=None, until=None,
         min_faves=None, max_age_hours=None)
```

The common X search operators are exposed as real arguments, and raw operators
still work inside `query`. `min_faves` is the most useful filter when you're
chasing a breaking claim and want to skip the noise. `max_age_hours` throws out a
result set whose newest post is already too old.

### `get_user_posts`

```
get_user_posts(username, limit=20, include_replies=False,
               media_only=False, exclude_retweets=False)
```

### `reader_status`

Health of the instance pool, broken down per capability: supported, stale, canary
age, usable, plus latency and cooldown. The `usable_by_capability` summary is the
useful bit. If it says `search: 1` then search is one bad response away from
failing and you should add an instance. Reach for this when results look empty,
wrong, or suspiciously old.

## Output

Posts come back as structured JSON:

```json
{
  "id": "2085790619968926027",
  "author": "@Reuters",
  "text": "OpenAI flags possible critical cybersecurity risk in upcoming model...",
  "created_at": "2026-08-07T18:10:10Z",
  "age_minutes": 9.3,
  "url": "https://x.com/Reuters/status/2085790619968926027",
  "is_retweet": false, "retweeted_by": null,
  "is_reply": false, "replying_to": null,
  "media": ["https://pbs.twimg.com/card_img/..."],
  "links": ["http://reut.rs/4yVIPEq"],
  "source_instance": "nitter.perennialte.ch"
}
```

`age_minutes` and the `x.com` URL are there so the agent can say how fresh a
claim is and cite it. Links are rewritten to `x.com` so citations don't rot when
an instance disappears.

Errors are returned as data, not raised: `{"ok": false, "error": ..., "hint": ...}`.

## How it copes with a broken fleet

A sweep of 45 public instances on 2026-08-07 turned up 2 that were fully usable.
They fail in five different ways, and each one needs handling:

| Failure | Seen on | Handling |
|---|---|---|
| Down | `nitter.privacydev.net`, DNS gone, 502 | circuit breaker, fail over |
| Bot wall | `nitter.space`, `poast`, `catsarch`: 403, Cloudflare, Anubis | validate by parsing, not by status code |
| Rate limit | `nitter.eu.org`, 429 | long cooldown, honours `Retry-After` |
| Feed switched off | `nitter.net`: timelines fine, `/search/rss` returns 403 "RSS feed is disabled" | retire that capability, keep the host |
| Stale content | `nitter.privacyredirect.com`: HTTP 200, valid RSS, 22 days old | canary freshness gating |

The last row is the one that bites a news agent, because nothing in the response
indicates a problem. You just get well-formed old headlines and repeat them as
breaking news.

To catch it, the pool probes a canary every 10 minutes and checks how old the
newest item is, against a fixed ceiling (180 min) and against the best instance
currently in the fleet. Laggards get marked stale and dropped. If every instance
for a capability is stale, the request fails and says why rather than quietly
handing back old news.

### Capabilities are tracked separately

Instances are not uniformly good or bad. They fail in parts:

```
nitter.privacyredirect.com   timeline: 22 days stale   search: 2 minutes fresh
nitter.net                   timeline: fresh           search: dead (empty 200)
farside.link/nitter          timeline: unreliable      search: fresh
```

Scoring a whole host on one capability meant a stale timeline disqualified a
working search mirror, which left exactly one usable search instance. Search then
died completely whenever it hiccuped. Scoring each capability on its own took
search from 1 usable mirror to 3, while `get_user_posts` still refuses the stale
timelines on that same host.

The same split applies to feeds an operator has switched off. Searching on
nitter.net works in a browser, so it looks like it should work here, but that is
the HTML UI. Its `/search/rss` answers 403 "RSS feed is disabled", which holds
even from inside a real browser session, so there is nothing an RSS client can
use. Worth knowing because the naive reading of that 403 is "host is down",
which would take nitter.net's perfectly good timelines offline along with it.

The rest of it:

- Candidates are tried in tiers: healthy, then cooling down, then capabilities
  believed dead, since those do come back. A run of failures used to shrink the
  candidate list below the retry budget and give up with mirrors left untried.
  Stale mirrors are never included at any tier.
- Transient failures (dropped connections, timeouts, 502/503/504) are retried on
  the same host before failing over. These are small servers and they blip a lot.
- A capability is only written off after two consecutive dead-looking responses.
  Working search mirrors are scarce, so one flaky empty reply shouldn't sideline
  one for ten minutes.
- Instances gate on User-Agent. A browser UA gets a challenge page or a
  `400 "only works inside an RSS client"`, so the client sends an RSS-reader UA.
  Worth knowing if you probe the fleet yourself: doing it with a browser UA will
  tell you half of it is dead when it is only refusing that UA.
- `xcancel.com` serves a feed that parses fine but is actually a "reader not
  whitelisted" notice. The parser rejects it explicitly.
- Ranking is Laplace-smoothed reliability minus latency, plus a capability bonus
  and a freshness penalty. Learned state is written to
  `~/.cache/x-mcp/reader/state.json` so a restart doesn't start from zero.
- These are volunteer-run servers, so requests to one host are spaced at least
  0.7s apart, concurrency is capped, and responses are cached for 60s.

## Configuration

All environment variables. The fleet churns constantly, so retuning shouldn't
need a code change.

| Variable | Default | Meaning |
|---|---|---|
| `X_MCP_READER_INSTANCES` | 8 seeded | Comma-separated base URLs |
| `X_MCP_READER_USER_AGENT` | Miniflux UA | Needs to look like an RSS reader |
| `X_MCP_READER_CANARY` | `Reuters` | Busy account used to probe timeline freshness |
| `X_MCP_READER_CANARY_SEARCH` | `news` | Constant-volume term used to probe search |
| `X_MCP_READER_MAX_CANARY_AGE_MIN` | `180` | Absolute staleness ceiling |
| `X_MCP_READER_MAX_CANARY_LAG_MIN` | `90` | Allowed lag behind the best instance |
| `X_MCP_READER_HEALTH_INTERVAL` | `600` | Seconds between health sweeps |
| `X_MCP_READER_CACHE_TTL` | `60` | Response cache lifetime |
| `X_MCP_READER_MIN_INTERVAL` | `0.7` | Minimum seconds between hits on one host |
| `X_MCP_READER_TIMEOUT` | `12` | Per-request timeout |
| `X_MCP_READER_MAX_ATTEMPTS` | `5` | Instances tried per logical request |
| `X_MCP_READER_RETRIES_PER_INSTANCE` | `1` | Same-host retries for transient failures |
| `X_MCP_READER_CAP_STRIKES` | `2` | Bad responses before writing off a capability |
| `X_MCP_READER_RATE_LIMIT_COOLDOWN` | `900` | Cooldown after a 429 |
| `X_MCP_READER_STATE_DIR` | `~/.cache/x-mcp/reader` | Where learned health state lives |

## Limitations

The instance list in `config.py` is a snapshot of what worked on 2026-08-07 and
will rot. The pool corrects itself at runtime, so a stale seed degrades rather
than breaks, but if results dry up run `reader_status` and add working hosts via
`X_MCP_READER_INSTANCES`. The reader's SSRF guard rejects loopback and private
addresses, so setting this variable to a local or private Nitter URL does not
work. Supporting a private mirror requires a narrowly scoped trusted-endpoint
change to the pool first.

Nitter's RSS doesn't carry like or retweet counts, so `min_faves` filters at
search time on the server but isn't returned per post.

Everything here is unverified user-generated content. The server tells the agent
as much in its instructions, and to prefer wire accounts for factual reporting.

## License

MIT
