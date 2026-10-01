"""Nitter RSS -> structured posts.

Nitter's feed is RSS 2.0 with a `dc:` creator extension. The useful bits:
  <title>       plain-text post body (URLs already expanded past t.co)
  <dc:creator>  @author of the original post
  <pubDate>     RFC 2822 timestamp
  <guid>        the numeric post id
  <link>        nitter permalink -> rewritten to x.com
  <description> CDATA HTML, the only place media lives
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree

DC = "{http://purl.org/dc/elements/1.1/}"

# "RT by @someone: actual text" / "R to @someone: actual text"
_RT_PREFIX = re.compile(r"^RT by @([A-Za-z0-9_]{1,15}):\s*")
_REPLY_PREFIX = re.compile(r"^R to @([A-Za-z0-9_]{1,15}):\s*")
_IMG_SRC = re.compile(r'<img[^>]+src="([^"]+)"', re.I)
_HREF = re.compile(r'<a[^>]+href="([^"]+)"', re.I)
_URL_IN_TEXT = re.compile(r"https?://\S+")


class ParseError(ValueError):
    """Body was not parseable Nitter RSS."""


@dataclass
class Post:
    id: str
    author: str
    text: str
    created_at: str          # ISO-8601 UTC
    age_minutes: float
    url: str                 # canonical x.com permalink
    is_retweet: bool = False
    retweeted_by: str | None = None
    is_reply: bool = False
    replying_to: str | None = None
    media: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    source_instance: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _clean_media_url(src: str) -> str | None:
    """Rewrite a nitter /pic/... proxy path back to the origin CDN URL."""
    try:
        path = urlparse(src).path
    except ValueError:
        return None
    marker = "/pic/"
    if marker not in path:
        return None
    tail = path.split(marker, 1)[1]
    # /pic/orig/media%2F... -> drop the size-variant segment
    if tail.startswith("orig/"):
        tail = tail[len("orig/"):]
    decoded = unquote(tail)
    if not decoded:
        return None
    if decoded.startswith(("http://", "https://")):
        return decoded
    if decoded.startswith(("pbs.twimg.com/", "video.twimg.com/", "abs.twimg.com/")):
        return "https://" + decoded
    return "https://pbs.twimg.com/" + decoded.lstrip("/")


def _canonical_url(link: str, author: str, post_id: str) -> str:
    """Rewrite a nitter permalink to x.com so links survive instance churn."""
    handle = author.lstrip("@")
    if post_id:
        return f"https://x.com/{handle or 'i'}/status/{post_id}"
    try:
        path = urlparse(link).path.rstrip("#m").lstrip("/")
    except ValueError:
        path = ""
    return f"https://x.com/{path}" if path else f"https://x.com/{handle}"


def _dedupe_trailing_urls(text: str) -> str:
    """Nitter often repeats a trailing URL (post link + link-card). Collapse it."""
    urls = _URL_IN_TEXT.findall(text)
    if len(urls) < 2:
        return text
    for u in set(urls):
        dup = f"{u} {u}"
        while dup in text:
            text = text.replace(dup, u)
    return text.strip()


def _post_id_from(guid: str, link: str) -> str:
    if guid and guid.isdigit():
        return guid
    m = re.search(r"/status/(\d+)", guid or "")
    if m:
        return m.group(1)
    m = re.search(r"/status/(\d+)", link or "")
    return m.group(1) if m else ""


def _text_of(el, tag: str) -> str:
    node = el.find(tag)
    return (node.text or "").strip() if node is not None else ""


def parse_feed(body: str, instance: str | None = None) -> tuple[list[Post], str]:
    """Parse a Nitter RSS body. Returns (posts, feed_title).

    Raises ParseError if the body is not RSS, which is how an instance quietly
    serving a bot-check page behind a 200 status gets caught.
    """
    if not body or not body.strip():
        raise ParseError("empty body")
    stripped = body.lstrip()
    if not stripped.startswith("<?xml") and "<rss" not in stripped[:400].lower():
        raise ParseError(f"not RSS (starts with: {stripped[:60]!r})")

    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        raise ParseError(f"malformed XML: {exc}") from exc

    channel = root.find("channel")
    if channel is None:
        raise ParseError("no <channel> element")

    feed_title = _text_of(channel, "title")

    # Whitelist/error feeds masquerade as real ones (xcancel does this).
    if "not yet whitelist" in feed_title.lower():
        raise ParseError(f"instance requires manual whitelisting: {feed_title}")

    now = datetime.now(timezone.utc)
    posts: list[Post] = []

    for item in channel.findall("item"):
        title = _text_of(item, "title")
        creator = _text_of(item, f"{DC}creator")
        link = _text_of(item, "link")
        guid = _text_of(item, "guid")
        pub = _text_of(item, "pubDate")

        try:
            created = parsedate_to_datetime(pub)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            created = created.astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue  # undated item is useless for a real-time feed

        text = title
        is_rt = False
        rt_by = None
        is_reply = False
        reply_to = None

        if m := _RT_PREFIX.match(text):
            is_rt, rt_by = True, "@" + m.group(1)
            text = text[m.end():]
        elif m := _REPLY_PREFIX.match(text):
            is_reply, reply_to = True, "@" + m.group(1)
            text = text[m.end():]

        desc_node = item.find("description")
        desc = desc_node.text or "" if desc_node is not None else ""
        media = [u for u in (_clean_media_url(s) for s in _IMG_SRC.findall(desc)) if u]
        links = [h for h in _HREF.findall(desc) if not h.startswith("http://nitter")]

        author = creator or "@" + (urlparse(link).path.lstrip("/").split("/")[0] or "unknown")
        post_id = _post_id_from(guid, link)

        posts.append(
            Post(
                id=post_id,
                author=author,
                text=_dedupe_trailing_urls(text.strip()),
                created_at=created.isoformat().replace("+00:00", "Z"),
                age_minutes=round((now - created).total_seconds() / 60.0, 1),
                url=_canonical_url(link, author, post_id),
                is_retweet=is_rt,
                retweeted_by=rt_by,
                is_reply=is_reply,
                replying_to=reply_to,
                media=media,
                links=links,
                source_instance=instance,
            )
        )

    return posts, feed_title


def newest_age_minutes(posts: list[Post]) -> float | None:
    """Age of the freshest post. This is the staleness signal for scoring."""
    if not posts:
        return None
    return min(p.age_minutes for p in posts)
