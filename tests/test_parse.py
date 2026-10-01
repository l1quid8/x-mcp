"""Parser tests -- pure, no network. Run: python -m unittest discover tests"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nitter_mcp.parse import ParseError, newest_age_minutes, parse_feed  # noqa: E402


def _feed(items: str, title: str = "Reuters / @Reuters") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:atom="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0">
  <channel><title>{title}</title><link>http://nitter.test/Reuters</link>
  {items}
  </channel></rss>"""


def _item(title, creator="@Reuters", pub=None, desc="", guid="123456789", link=None):
    pub = pub or datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    link = link or f"http://nitter.test/Reuters/status/{guid}#m"
    return f"""<item><title>{title}</title><dc:creator>{creator}</dc:creator>
    <description><![CDATA[{desc}]]></description>
    <pubDate>{pub}</pubDate>
    <guid isPermaLink="false">{guid}</guid><link>{link}</link></item>"""


class TestParse(unittest.TestCase):
    def test_basic_fields(self):
        posts, title = parse_feed(_feed(_item("Hello world")), instance="nitter.test")
        self.assertEqual(len(posts), 1)
        p = posts[0]
        self.assertEqual(p.text, "Hello world")
        self.assertEqual(p.author, "@Reuters")
        self.assertEqual(p.id, "123456789")
        self.assertEqual(p.url, "https://x.com/Reuters/status/123456789")
        self.assertEqual(p.source_instance, "nitter.test")
        self.assertLess(p.age_minutes, 2)

    def test_retweet_prefix_stripped(self):
        posts, _ = parse_feed(_feed(_item("RT by @someone: real content here", creator="@origauthor")))
        p = posts[0]
        self.assertTrue(p.is_retweet)
        self.assertEqual(p.retweeted_by, "@someone")
        self.assertEqual(p.author, "@origauthor")
        self.assertEqual(p.text, "real content here")

    def test_reply_prefix_stripped(self):
        posts, _ = parse_feed(_feed(_item("R to @bob: my answer")))
        self.assertTrue(posts[0].is_reply)
        self.assertEqual(posts[0].replying_to, "@bob")
        self.assertEqual(posts[0].text, "my answer")

    def test_media_url_decoded_to_cdn(self):
        desc = '<p>x</p><img src="http://nitter.test/pic/media%2FABC123.jpg" />'
        posts, _ = parse_feed(_feed(_item("with pic", desc=desc)))
        self.assertEqual(posts[0].media, ["https://pbs.twimg.com/media/ABC123.jpg"])

    def test_media_orig_variant_and_absolute_host(self):
        desc = ('<img src="http://n.test/pic/orig/media%2FZZ.png" />'
                '<img src="http://n.test/pic/pbs.twimg.com%2Fcard_img%2F9%2Fk.jpg" />')
        posts, _ = parse_feed(_feed(_item("m", desc=desc)))
        self.assertEqual(posts[0].media, [
            "https://pbs.twimg.com/media/ZZ.png",
            "https://pbs.twimg.com/card_img/9/k.jpg",
        ])

    def test_trailing_duplicate_url_collapsed(self):
        posts, _ = parse_feed(_feed(_item("Story http://reut.rs/abc http://reut.rs/abc")))
        self.assertEqual(posts[0].text, "Story http://reut.rs/abc")

    def test_undated_item_skipped(self):
        posts, _ = parse_feed(_feed(_item("no date", pub="not-a-date") + _item("good")))
        self.assertEqual([p.text for p in posts], ["good"])

    def test_age_minutes_computed(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        posts, _ = parse_feed(_feed(_item("old news", pub=old)))
        self.assertAlmostEqual(posts[0].age_minutes, 180, delta=3)

    def test_newest_age_uses_freshest(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        posts, _ = parse_feed(_feed(_item("old", pub=old, guid="1") + _item("new", guid="2")))
        self.assertLess(newest_age_minutes(posts), 2)

    # --- the failure modes that matter for reliability --------------------

    def test_bot_wall_html_rejected(self):
        with self.assertRaises(ParseError):
            parse_feed("<!doctype html><html><title>Making sure you're not a bot!</title></html>")

    def test_empty_body_rejected(self):
        with self.assertRaises(ParseError):
            parse_feed("")

    def test_malformed_xml_rejected(self):
        with self.assertRaises(ParseError):
            parse_feed("<?xml version='1.0'?><rss><channel><title>x</title>")

    def test_whitelist_feed_rejected(self):
        """xcancel returns a valid-looking feed that is really an error page."""
        body = _feed(_item("RSS reader not yet whitelisted!"), title="RSS reader not yet whitelisted!")
        with self.assertRaises(ParseError):
            parse_feed(body)

    def test_empty_but_valid_feed_is_not_an_error(self):
        posts, title = parse_feed(_feed(""))
        self.assertEqual(posts, [])
        self.assertEqual(title, "Reuters / @Reuters")


if __name__ == "__main__":
    unittest.main(verbosity=2)
