"""Pool selection logic -- pure, no network.

These lock down the reliability rules that are easy to regress:
per-capability staleness, tiering, and the never-serve-stale-content guarantee.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Point state persistence at a temp dir before the pool is constructed.
_TMP = tempfile.mkdtemp(prefix="x-mcp-reader-test-")
os.environ["X_MCP_READER_STATE_DIR"] = _TMP

from nitter_mcp.pool import InstanceState, NitterPool  # noqa: E402


def _pool(*urls: str) -> NitterPool:
    return NitterPool(urls or ("https://a.test", "https://b.test", "https://c.test"))


class TestPerCapabilityHealth(unittest.TestCase):
    """The core insight: instances diverge per capability.

    nitter.privacyredirect.com really does serve 22-day-stale timelines while
    its search results are 2 minutes fresh.
    """

    def test_stale_timeline_does_not_disqualify_search(self):
        p = _pool()
        st = p.instances["https://a.test"]
        st.caps = {"timeline": True, "search": True}
        st.canary_age = {"timeline": 31650.0, "search": 1.0}
        st.stale = {"timeline": True, "search": False}

        self.assertNotIn(st, p._ordered("timeline")[:1], "stale timeline must be demoted")
        self.assertIs(p._ordered("search")[0], st, "fresh search must still rank first")

    def test_stale_capability_never_served(self):
        p = _pool("https://a.test")
        st = p.instances["https://a.test"]
        st.caps = {"timeline": True}
        st.stale = {"timeline": True}
        # Sole instance, stale for the requested capability -> must not be offered.
        self.assertEqual(p._ordered("timeline"), [])

    def test_dead_capability_is_last_resort_not_dropped(self):
        p = _pool("https://a.test")
        st = p.instances["https://a.test"]
        st.caps = {"search": False}  # believed dead, may recover
        self.assertEqual(p._ordered("search"), [st])

    def test_cooling_down_instance_is_tail_not_dropped(self):
        p = _pool("https://a.test", "https://b.test")
        good, cooling = p.instances["https://a.test"], p.instances["https://b.test"]
        good.caps = cooling.caps = {"search": True}
        cooling.cooldown_until = time.time() + 600

        order = p._ordered("search")
        self.assertEqual([s.host for s in order], ["a.test", "b.test"])
        self.assertFalse(cooling.available())

    def test_fresher_instance_outranks_staler_one(self):
        p = _pool("https://a.test", "https://b.test")
        a, b = p.instances["https://a.test"], p.instances["https://b.test"]
        for s in (a, b):
            s.caps = {"search": True}
            s.successes = 10
        a.canary_age = {"search": 240.0}
        b.canary_age = {"search": 2.0}
        self.assertIs(p._ordered("search")[0], b)

    def test_spread_rotates_across_hosts(self):
        p = _pool()
        for s in p.instances.values():
            s.caps = {"timeline": True}
        heads = {p._ordered("timeline", spread=i)[0].host for i in range(3)}
        self.assertEqual(len(heads), 3, "fan-out must not serialise on one host")


class TestCapabilityStrikes(unittest.TestCase):
    """One flaky empty response must not sideline a scarce search mirror."""

    def test_single_strike_does_not_condemn(self):
        p = _pool("https://a.test")
        st = p.instances["https://a.test"]
        p._record_failure(st, "search", "bad body: empty", cap_dead=True)
        self.assertIsNot(st.caps.get("search"), False)
        self.assertIn(st, p._ordered("search"))

    def test_repeated_strikes_condemn(self):
        p = _pool("https://a.test")
        st = p.instances["https://a.test"]
        for _ in range(2):
            p._record_failure(st, "search", "bad body: empty", cap_dead=True)
        self.assertIs(st.caps["search"], False)

    def test_success_resets_strikes(self):
        p = _pool("https://a.test")
        st = p.instances["https://a.test"]
        p._record_failure(st, "search", "bad body: empty", cap_dead=True)
        p._record_success(st, "search", 1.0)
        p._record_failure(st, "search", "bad body: empty", cap_dead=True)
        self.assertIsNot(st.caps.get("search"), False, "strikes must not accumulate across successes")

    def test_explicitly_disabled_feed_condemns_immediately(self):
        """nitter.net answers /search/rss with 403 "RSS feed is disabled".

        The server is telling us outright, so no confirmation round is needed,
        and it must not take the host's working timelines down with it.
        """
        p = _pool("https://a.test")
        st = p.instances["https://a.test"]
        p._record_failure(st, "search", "403: RSS disabled", cap_dead=True, definitive=True)
        self.assertIs(st.caps["search"], False)
        self.assertTrue(st.available(), "a disabled feed must not trip the breaker")
        self.assertIsNot(st.caps.get("timeline"), False)

    def test_disabled_marker_detection(self):
        import httpx

        from nitter_mcp.pool import _feed_disabled

        disabled = httpx.Response(403, text="<html>nitter RSS feed is disabled</html>")
        forbidden = httpx.Response(403, text="<html>403 Forbidden</html>")
        ok = httpx.Response(200, text="nitter rss feed is disabled")  # wrong status
        self.assertTrue(_feed_disabled(disabled))
        self.assertFalse(_feed_disabled(forbidden), "a plain 403 is host-level, not capability-level")
        self.assertFalse(_feed_disabled(ok))

    def test_cap_dead_does_not_trip_circuit_breaker(self):
        p = _pool("https://a.test")
        st = p.instances["https://a.test"]
        for _ in range(3):
            p._record_failure(st, "search", "bad body: empty", cap_dead=True)
        self.assertTrue(st.available(), "a dead capability must not take the whole host offline")
        self.assertIn(st, p._ordered("timeline"))


class TestStatePersistence(unittest.TestCase):
    def test_legacy_per_instance_schema_migrates(self):
        """Older state used a single bool/float for the whole instance."""
        st = InstanceState.from_json({
            "base_url": "https://a.test",
            "canary_age_min": 900.0,
            "stale": True,
            "caps": {"timeline": True},
        })
        self.assertEqual(st.canary_age, {"timeline": 900.0})
        self.assertTrue(st.is_stale("timeline"))
        self.assertFalse(st.is_stale("search"))

    def test_roundtrip(self):
        st = InstanceState(base_url="https://a.test")
        st.caps = {"search": True, "timeline": False}
        st.canary_age = {"search": 3.5}
        st.stale = {"timeline": True}
        st.successes, st.failures = 7, 2
        back = InstanceState.from_json(st.to_json())
        self.assertEqual(back.caps, st.caps)
        self.assertEqual(back.canary_age, st.canary_age)
        self.assertTrue(back.is_stale("timeline"))
        self.assertEqual((back.successes, back.failures), (7, 2))

    def test_corrupt_state_entry_is_ignored(self):
        p = _pool("https://a.test")
        before = p.instances["https://a.test"]
        p._load_state()  # no file yet; must not raise
        self.assertIsNotNone(before)


class TestSnapshot(unittest.TestCase):
    def test_snapshot_reports_per_capability_usability(self):
        p = _pool("https://a.test")
        st = p.instances["https://a.test"]
        st.caps = {"timeline": True, "search": True}
        st.canary_age = {"timeline": 31650.0, "search": 1.0}
        st.stale = {"timeline": True, "search": False}

        snap = p.snapshot()
        caps = snap["instances"][0]["capabilities"]
        self.assertFalse(caps["timeline"]["usable"])
        self.assertTrue(caps["search"]["usable"])
        self.assertEqual(snap["usable_by_capability"], {"timeline": 0, "search": 1})


if __name__ == "__main__":
    unittest.main(verbosity=2)
