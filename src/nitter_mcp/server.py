"""Public X reader functions registered on the shared MCP server."""

from __future__ import annotations

import asyncio
import inspect
import re
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations

from . import config
from .parse import Post
from .pool import AllInstancesFailed, NitterPool

MAX_LIMIT = 100
MAX_SOURCES = 25
HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}")


def _provenance() -> dict:
    return {
        "source": "public_nitter_rss",
        "verification": "unverified_public_posts",
    }


def _accounts(sources: list[str]) -> list[str] | None:
    """Validate and deduplicate custom sources before any network fan-out."""
    if len(sources) > MAX_SOURCES:
        return None
    accounts: list[str] = []
    seen: set[str] = set()
    for source in sources:
        if not isinstance(source, str):
            return None
        handle = source.strip().lstrip("@")
        if not HANDLE.fullmatch(handle):
            return None
        key = handle.casefold()
        if key not in seen:
            accounts.append(handle)
            seen.add(key)
    return accounts


def _posts_payload(posts: list[Post], limit: int) -> list[dict]:
    return [p.to_dict() for p in posts[:limit]]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _build_query(
    query: str,
    from_user: str | None,
    exclude_retweets: bool,
    exclude_replies: bool,
    lang: str | None,
    since: str | None,
    until: str | None,
    min_faves: int | None,
) -> str:
    parts = [query.strip()] if query and query.strip() else []
    if from_user:
        parts.append(f"from:{from_user.lstrip('@')}")
    if exclude_retweets:
        parts.append("-filter:retweets")
    if exclude_replies:
        parts.append("-filter:replies")
    if lang:
        parts.append(f"lang:{lang}")
    if since:
        parts.append(f"since:{since}")
    if until:
        parts.append(f"until:{until}")
    if min_faves:
        parts.append(f"min_faves:{int(min_faves)}")
    return " ".join(parts).strip()


def _error(msg: str, hint: str | None = None) -> dict:
    out = {"ok": False, "error": msg, "fetched_at": _now_iso()}
    if hint:
        out["hint"] = hint
    return out


async def search_x(
    read_pool: NitterPool,
    query: str = "",
    limit: int = 20,
    from_user: str | None = None,
    exclude_retweets: bool = True,
    exclude_replies: bool = False,
    lang: str | None = None,
    since: str | None = None,
    until: str | None = None,
    min_faves: int | None = None,
    max_age_hours: float | None = None,
) -> dict:
    """Search X/Twitter.

    Args:
        query: Free-text search. Quote a phrase for exact match ('"rate cut"').
        limit: Max posts to return (1-100).
        from_user: Restrict to one account, without the @.
        exclude_retweets: Drop retweets. Defaults to True because retweet spam
            dominates raw search results.
        exclude_replies: Drop replies to other posts.
        lang: Two-letter language filter, e.g. "en".
        since: Only posts on/after this date, YYYY-MM-DD.
        until: Only posts before this date, YYYY-MM-DD.
        min_faves: Minimum like count. Good quality filter for breaking claims.
        max_age_hours: Reject the result set if its newest post is older than this.
    """
    limit = max(1, min(int(limit), MAX_LIMIT))
    # Guard on the *subject* of the search, not the assembled string: the default
    # filters alone would otherwise make an empty query look non-empty.
    if not (query or "").strip() and not (from_user or "").strip():
        return _error("empty query", "pass `query` text or a `from_user`")
    q = _build_query(query, from_user, exclude_retweets, exclude_replies, lang, since, until, min_faves)

    path = f"/search/rss?f=tweets&q={quote(q)}"
    max_age = max_age_hours * 60 if max_age_hours else None

    try:
        posts, _, host = await read_pool.fetch(
            path, "search", require_items=True, max_age_minutes=max_age
        )
    except AllInstancesFailed as exc:
        return _error(str(exc), "run reader_status to inspect the mirror pool")

    if exclude_retweets:
        posts = [p for p in posts if not p.is_retweet]

    return {
        "ok": True,
        "query": q,
        "count": min(len(posts), limit),
        "served_by": host,
        "fetched_at": _now_iso(),
        "provenance": _provenance(),
        "posts": _posts_payload(posts, limit),
    }


async def get_user_posts(
    read_pool: NitterPool,
    username: str,
    limit: int = 20,
    include_replies: bool = False,
    media_only: bool = False,
    exclude_retweets: bool = False,
    allow_search_fallback: bool = True,
) -> dict:
    """Get an account's timeline.

    Args:
        username: Handle, with or without the leading @.
        limit: Max posts to return (1-100).
        include_replies: Include the account's replies to others.
        media_only: Only posts containing images or video.
        exclude_retweets: Drop retweets from the timeline.
        allow_search_fallback: Use account search if timeline RSS fails or is empty.
            Fallback results are partial and do not prove which post is latest on X.
    """
    handle = username.strip().lstrip("@")
    if not HANDLE.fullmatch(handle):
        return _error(f"invalid username: {username!r}")

    limit = max(1, min(int(limit), MAX_LIMIT))
    if media_only:
        path = f"/{handle}/media/rss"
    elif include_replies:
        path = f"/{handle}/with_replies/rss"
    else:
        path = f"/{handle}/rss"

    timeline_error = None
    retrieval_method = "timeline"
    try:
        # require_items=False: a real account can legitimately have no posts.
        posts, title, host = await read_pool.fetch(path, "timeline", require_items=False)
    except AllInstancesFailed as exc:
        if not allow_search_fallback:
            return _error(str(exc), f"@{handle} may not exist, or the pool is degraded")
        timeline_error = str(exc)
        posts = []

    if not posts and allow_search_fallback:
        timeline_error = timeline_error or "timeline RSS returned no posts"
        query = _build_query(
            "", handle, exclude_retweets, not include_replies,
            None, None, None, None,
        )
        if media_only:
            query += " filter:media"
        try:
            posts, title, host = await read_pool.fetch(
                f"/search/rss?f=tweets&q={quote(query)}", "search", require_items=True,
            )
        except AllInstancesFailed as exc:
            result = _error(
                "timeline and account-search fallback both failed",
                "run reader_status to inspect the public mirror pool",
            )
            result.update(timeline_error=timeline_error, search_error=str(exc))
            return result

        # Search mirrors can ignore operators. Verify account and filters locally
        # before treating their response as this user's posts.
        posts = [p for p in posts if (
            p.author.lstrip("@").casefold() == handle.casefold()
            and (include_replies or not p.is_reply)
            and (not media_only or p.media)
            and (not exclude_retweets or not p.is_retweet)
        )]
        if not posts:
            result = _error(
                "timeline unavailable and account search returned no matching posts",
                "an empty search does not establish that the account has no posts",
            )
            result.update(timeline_error=timeline_error, search_instance=host)
            return result
        posts.sort(key=lambda p: p.created_at, reverse=True)
        retrieval_method = "search_fallback"

    if exclude_retweets:
        posts = [p for p in posts if not p.is_retweet]

    result = {
        "ok": True,
        "account": f"@{handle}",
        "feed_title": title,
        "count": min(len(posts), limit),
        "served_by": host,
        "fetched_at": _now_iso(),
        "provenance": _provenance(),
        "posts": _posts_payload(posts, limit),
        "retrieval_method": retrieval_method,
    }
    if retrieval_method == "search_fallback":
        result.update(
            partial=True,
            timeline_error=timeline_error,
            warning=(
                "Timeline RSS was unavailable or empty. These are account-search "
                "results and may omit posts and reposts. The newest returned post "
                "is not guaranteed to be the account's latest post on X."
            ),
        )
    return result


async def get_breaking_news(
    read_pool: NitterPool,
    topic: str = config.DEFAULT_TOPIC,
    query: str | None = None,
    limit: int = 30,
    within_hours: float = 12.0,
    sources: list[str] | None = None,
) -> dict:
    """Merge recent posts from newswire accounts, newest first.

    Args:
        topic: One of world, us, business, tech, ai, crypto, science, sports.
        query: Optional keyword to filter headlines (case-insensitive substring).
        limit: Max posts to return (1-100).
        within_hours: Only include posts newer than this.
        sources: Override the curated account list with your own handles.
    """
    limit = max(1, min(int(limit), MAX_LIMIT))
    topic_key = (topic or config.DEFAULT_TOPIC).lower().strip()

    if sources is not None:
        accounts = _accounts(sources)
        if not accounts:
            return _error(
                "invalid sources",
                f"pass 1-{MAX_SOURCES} valid X handles, each at most 15 letters, digits, or underscores",
            )
    else:
        accounts = config.TOPIC_SOURCES.get(topic_key)
        if not accounts:
            return _error(
                f"unknown topic {topic!r}",
                f"valid topics: {', '.join(sorted(config.TOPIC_SOURCES))}, or pass `sources`",
            )

    async def one(idx: int, handle: str):
        try:
            posts, _, host = await read_pool.fetch(
                f"/{handle}/rss", "timeline", require_items=False, spread=idx
            )
            return handle, posts, host, None
        except AllInstancesFailed as exc:
            return handle, [], None, str(exc)

    results = await asyncio.gather(*(one(i, h) for i, h in enumerate(accounts)))

    merged: dict[str, Post] = {}
    reached: list[str] = []
    failed: dict[str, str] = {}
    for handle, posts, host, err in results:
        if err:
            failed[handle] = err
            continue
        reached.append(handle)
        for p in posts:
            if p.is_retweet or p.age_minutes > within_hours * 60:
                continue
            key = p.id or f"{p.author}:{p.text[:60]}"
            merged.setdefault(key, p)

    if not reached:
        return _error(
            "could not reach any newswire source",
            "run reader_status to inspect the mirror pool",
        )

    posts_out = sorted(merged.values(), key=lambda p: p.age_minutes)

    if query:
        needle = query.lower()
        posts_out = [p for p in posts_out if needle in p.text.lower()]

    return {
        "ok": True,
        "topic": topic_key if not sources else "custom",
        "query": query,
        "sources_used": reached,
        "sources_failed": failed or None,
        "window_hours": within_hours,
        "count": min(len(posts_out), limit),
        "fetched_at": _now_iso(),
        "provenance": _provenance(),
        "posts": _posts_payload(posts_out, limit),
    }


async def reader_status(read_pool: NitterPool, recheck: bool = False) -> dict:
    """Inspect pool health.

    Args:
        recheck: Force an immediate health sweep instead of using cached state.
    """
    if recheck:
        read_pool.clear_cache()
    await read_pool.ensure_health(force=recheck)
    snap = read_pool.snapshot()
    usable = snap["usable_by_capability"]
    snap.update({
        "ok": any(usable.values()),
        "total_instances": len(snap["instances"]),
        "fetched_at": _now_iso(),
        "provenance": _provenance(),
        "note": (
            "Health is tracked per capability: an instance can serve fresh search "
            "results while its timelines are weeks stale, so each capability is "
            "scored and gated independently. 'stale' means the instance returns "
            "HTTP 200 with valid but outdated RSS."
        ),
    })
    return snap


def register_read_tools(
    target: MCPServer,
    authorize_read: Callable[[Context], object],
    read_pool: NitterPool,
) -> None:
    """Attach read tools to the shared MCP server with an injected reader pool."""
    if not callable(authorize_read):
        raise TypeError("authorize_read must be a callable")
    if read_pool is None:
        raise TypeError("read_pool is required")

    annotations = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
    tool_meta = {"securitySchemes": [{"type": "oauth2", "scopes": ["x:read"]}]}

    async def invoke(ctx: Context, operation: Callable, *args, **kwargs) -> dict[str, Any]:
        # Authorize before any reader operation or network request.
        if ctx is None:
            raise PermissionError("x:read is required")
        verdict = authorize_read(ctx)
        if inspect.isawaitable(verdict):
            await verdict
        return await operation(read_pool, *args, **kwargs)

    @target.tool(
        name="search_x",
        description="Search public X posts through public mirrors. Results are unverified and include post age and provenance.",
        structured_output=True,
        annotations=annotations,
        meta=tool_meta,
    )
    async def registered_search_x(
        ctx: Context,
        query: str = "",
        limit: int = 20,
        from_user: str | None = None,
        exclude_retweets: bool = True,
        exclude_replies: bool = False,
        lang: str | None = None,
        since: str | None = None,
        until: str | None = None,
        min_faves: int | None = None,
        max_age_hours: float | None = None,
    ) -> dict[str, Any]:
        return await invoke(
            ctx, search_x, query, limit, from_user, exclude_retweets,
            exclude_replies, lang, since, until, min_faves, max_age_hours,
        )

    @target.tool(
        name="get_user_posts",
        description="Read an account's public timeline through public mirrors; fallback search is marked partial.",
        structured_output=True,
        annotations=annotations,
        meta=tool_meta,
    )
    async def registered_get_user_posts(
        username: str,
        ctx: Context,
        limit: int = 20,
        include_replies: bool = False,
        media_only: bool = False,
        exclude_retweets: bool = False,
        allow_search_fallback: bool = True,
    ) -> dict[str, Any]:
        return await invoke(
            ctx, get_user_posts, username, limit, include_replies,
            media_only, exclude_retweets, allow_search_fallback,
        )

    @target.tool(
        name="get_breaking_news",
        description="Merge recent public posts from selected news accounts through public mirrors; claims remain unverified.",
        structured_output=True,
        annotations=annotations,
        meta=tool_meta,
    )
    async def registered_get_breaking_news(
        ctx: Context,
        topic: str = config.DEFAULT_TOPIC,
        query: str | None = None,
        limit: int = 30,
        within_hours: float = 12.0,
        sources: list[str] | None = None,
    ) -> dict[str, Any]:
        return await invoke(ctx, get_breaking_news, topic, query, limit, within_hours, sources)

    @target.tool(
        name="reader_status",
        description="Inspect public mirror health and freshness by capability.",
        structured_output=True,
        annotations=annotations,
        meta=tool_meta,
    )
    async def registered_reader_status(ctx: Context, recheck: bool = False) -> dict[str, Any]:
        return await invoke(ctx, reader_status, recheck)
