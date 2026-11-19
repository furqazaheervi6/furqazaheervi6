import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import update_readme as u  # noqa: E402

UTC = dt.timezone.utc


def proj(repo="o/ok", **kw):
    d = {"repo": repo, "enabled": True, "name": "N", "state": "Built-unpublished", "summary": "S"}
    d.update(kw)
    return d


class Validation(unittest.TestCase):
    def test_proposed_refused(self):
        with self.assertRaises(u.ConfigError):
            u.validate_projects({"projects": [proj(state="Proposed")]})

    def test_unknown_state_refused(self):
        with self.assertRaises(u.ConfigError):
            u.validate_projects({"projects": [proj(state="Done")]})

    def test_disabled_entries_are_skipped_even_if_incomplete(self):
        out = u.validate_projects({"projects": [proj(), {"repo": "o/held", "enabled": False}]})
        self.assertEqual([p["repo"] for p in out], ["o/ok"])

    def test_duplicate_and_bad_repo(self):
        with self.assertRaises(u.ConfigError):
            u.validate_projects({"projects": [proj(), proj()]})
        with self.assertRaises(u.ConfigError):
            u.validate_projects({"projects": [proj(repo="not a repo")]})

    def test_real_config_loads(self):
        cfg = u.load_json(u.PROJECTS_JSON)
        repos = [p["repo"] for p in u.validate_projects(cfg)]
        self.assertTrue(repos)  # at least one allowlisted project


class Rendering(unittest.TestCase):
    def test_now(self):
        out = u.render_now({"updated": "2026-11", "items": [{"role": "A", "org": "B", "detail": "C"}]})
        self.assertIn("- **A** at B. C", out)
        self.assertEqual(u.render_now({"items": []}), "")

    def test_empty_blocks_render_nothing(self):
        self.assertEqual(u.render_featured([]), "")
        self.assertEqual(u.render_activity([], 5), "")
        self.assertEqual(u.render_writing([]), "")

    def test_escape_blocks_injection(self):
        s = u.md_escape("fix [x](http://evil) <img> @someone (#12) `code`")
        self.assertNotIn("](", s)
        self.assertNotIn("<img>", s)
        self.assertNotIn("@s", s)
        self.assertNotIn("#12", s)

    def test_activity_sorted_limited_and_stable(self):
        mk = lambda d, sha: {"when": dt.datetime(2026, 11, d, tzinfo=UTC), "repo": "o/r", "sha": sha * 40,
                             "message": f"m{d}", "url": f"https://github.com/o/r/commit/{sha}"}
        cs = [mk(d, str(d % 10)) for d in range(1, 9)]
        out = u.render_activity(cs, 3)
        self.assertEqual(out.count("\n- "), 3)
        self.assertLess(out.index("m8"), out.index("m7"))
        self.assertEqual(out, u.render_activity(list(reversed(cs)), 3))

    FEED = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
    <item><title>Old</title><link>https://example.com/p/old</link><pubDate>Mon, 03 Nov 2026 10:00:00 GMT</pubDate></item>
    <item><title>New [post]</title><link>https://example.com/p/new</link><pubDate>Tue, 17 Nov 2026 10:00:00 GMT</pubDate></item>
    <item><title>Offsite</title><link>https://evil.example/x</link><pubDate>Tue, 17 Nov 2026 10:00:00 GMT</pubDate></item>
    </channel></rss>"""

    def test_feed_parse_order_and_link_filter(self):
        posts = u.parse_feed(self.FEED, "https://example.com/", 5)
        self.assertEqual([p["title"] for p in posts], ["New [post]", "Old"])
        out = u.render_writing(posts)
        self.assertIn("New \\[post\\]", out)
        self.assertNotIn("evil", out)

    def test_empty_feed_renders_nothing(self):
        empty = b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title></channel></rss>'
        self.assertEqual(u.render_writing(u.parse_feed(empty, "https://x/", 5)), "")

    def test_feed_with_entities_refused(self):
        bad = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "x">]><rss><channel/></rss>'
        with self.assertRaises(u.ConfigError):
            u.parse_feed(bad, "https://x/", 5)


class Splice(unittest.TestCase):
    TEXT = "a\n<!-- NOW:START -->\nold\n<!-- NOW:END -->\nb\n"

    def test_set_and_clear(self):
        t = u.set_block(self.TEXT, "NOW", "new")
        self.assertIn("\nnew\n", t)
        self.assertEqual(u.get_block(t, "NOW"), "new")
        cleared = u.set_block(t, "NOW", "")
        self.assertNotIn("new", cleared)
        self.assertIn("<!-- NOW:START -->", cleared)

    def test_idempotent(self):
        once = u.set_block(self.TEXT, "NOW", "new")
        self.assertEqual(once, u.set_block(once, "NOW", "new"))

    def test_missing_marker(self):
        with self.assertRaises(u.ConfigError):
            u.set_block("no markers", "NOW", "x")

    def test_offline_build_on_real_readme_is_idempotent(self):
        text = u.README.read_text(encoding="utf-8")
        now, cfg = u.load_json(u.NOW_JSON), u.load_json(u.PROJECTS_JSON)
        once = u.build(text, now, cfg, offline=True, token=None)
        self.assertEqual(once, u.build(once, now, cfg, offline=True, token=None))
        self.assertNotIn("Traceback", once)


if __name__ == "__main__":
    unittest.main()
