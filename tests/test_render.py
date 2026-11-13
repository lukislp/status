import json
import re
import tempfile
import unittest
from datetime import timedelta
from html.parser import HTMLParser
from pathlib import Path

from statuspage import model, render
from statuspage.manual import parse_incident

from .test_manual import INCIDENT, MAINTENANCE
from .test_model import T0, config, fail, ok

ROOT = Path(__file__).resolve().parent.parent
VOID = {"meta", "link", "br", "img", "input", "hr", "path", "rect"}


class TagBalance(HTMLParser):
    """Fails on unclosed or mismatched elements - the classic hand-template mistake."""

    def __init__(self):
        super().__init__()
        self.stack = []
        self.errors = []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unexpected </{tag}>, open: {self.stack[-3:]}")
            return
        self.stack.pop()


def build(data, manual, now):
    out = Path(tempfile.mkdtemp()) / "_site"
    render.build_site(config(), data, manual, now, ROOT / "site", out)
    return out


def sample_data():
    data = model.empty_data()
    cfg = config()
    now = T0
    for n, web in enumerate([ok(), fail(), fail(), ok(), ok()]):
        data, _ = model.apply_run(data, cfg, {"web": [web], "app": [ok(), ok()]}, now + timedelta(minutes=5 * n))
    return data


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.now = T0 + timedelta(hours=8)
        manual = [parse_incident(INCIDENT, "2026-10-30-login", {"web", "app"}),
                  parse_incident(MAINTENANCE, "db-upgrade", {"web", "app"}, "maintenance")]
        self.root = build(sample_data(), manual, self.now)
        self.out = self.root / "en"  # the English tree; the German one is checked in test_i18n.py

    def test_expected_files(self):
        for name in ["index.html", "history.html", "feed.xml", "incidents/2026-10-30-login.html",
                     "incidents/db-upgrade.html"]:
            self.assertTrue((self.root / name).is_file(), name)  # German at the root
            self.assertTrue((self.out / name).is_file(), f"en/{name}")  # English under /en/
        for name in ["404.html", "status.json", "CNAME", "style.css", "bars.js", "favicon.svg"]:
            self.assertTrue((self.root / name).is_file(), name)
        self.assertFalse((self.out / "status.json").exists())  # one language-neutral status.json
        self.assertEqual((self.root / "CNAME").read_text().strip(), "status.example.org")

    def test_html_is_balanced(self):
        for path in self.root.rglob("*.html"):
            parser = TagBalance()
            parser.feed(path.read_text(encoding="utf-8"))
            self.assertEqual(parser.errors, [], path.name)
            self.assertEqual(parser.stack, [], path.name)

    def test_no_third_party_requests(self):
        pattern = re.compile(r"""(?:src|href)\s*=\s*["'](?:https?:)?//|url\(\s*["']?(?:https?:)?//|@import""")
        for path in self.root.rglob("*"):
            if path.suffix not in (".html", ".css", ".js"):
                continue
            text = path.read_text(encoding="utf-8")
            # Plain navigation links (<a href>) are fine: they are not loaded by the page.
            text = re.sub(r"<a\s[^>]*>", "", text)
            text = re.sub(r'<link rel="(?:alternate|canonical)"[^>]*>', "", text)
            self.assertIsNone(pattern.search(text), path.name)

    def test_ninety_bars_per_component(self):
        index = (self.out / "index.html").read_text(encoding="utf-8")
        self.assertEqual(index.count('class="bar '), 90 * 3)
        self.assertEqual(index.count('tabindex="0" aria-label='), 3)  # one tab stop per component

    def test_uptime_30d_only_counts_the_last_thirty_days(self):
        data = sample_data()
        old = (self.now - timedelta(days=45)).strftime("%Y-%m-%d")
        data["history"]["days"]["web"][old] = {"n": 100, "up": 0.0, "fail": 100}
        doc = json.loads((build(data, [], self.now) / "status.json").read_text(encoding="utf-8"))
        web = {c["id"]: c for c in doc["components"]}["web"]
        self.assertAlmostEqual(web["uptime_30d"], 60.0)  # the failed day 45 days ago is outside
        self.assertLess(web["uptime_90d"], 60.0)  # but inside the 90-day window

    def test_status_reflects_manual_incident_and_planned(self):
        doc = json.loads((self.root / "status.json").read_text(encoding="utf-8"))
        by_id = {c["id"]: c for c in doc["components"]}
        self.assertEqual(by_id["soon"]["status"], "planned")
        self.assertIsNone(by_id["soon"]["uptime_90d"])
        self.assertEqual(by_id["web"]["status"], "operational")
        self.assertAlmostEqual(by_id["web"]["uptime_90d"], 60.0)
        self.assertAlmostEqual(by_id["web"]["uptime_30d"], 60.0)
        self.assertIsNone(by_id["soon"]["uptime_30d"])
        self.assertEqual(doc["status"]["indicator"], "operational")  # login incident is resolved
        self.assertEqual(doc["incidents"], [])
        self.assertEqual([(m["id"], m["state"]) for m in doc["maintenance"]], [("db-upgrade", "scheduled")])

    def test_maintenance_section(self):
        index = (self.out / "index.html").read_text(encoding="utf-8")
        self.assertIn('class="maintenance"', index)
        self.assertIn('class="mw mw-scheduled"', index)
        self.assertIn("Mon, Nov 2 &middot; 02:00&ndash;03:00 UTC", index)
        # No maintenance files -> no section at all.
        out = build(sample_data(), [], self.now)
        self.assertNotIn('class="maintenance"', (out / "en" / "index.html").read_text(encoding="utf-8"))

    def test_running_maintenance_sets_banner(self):
        manual = [parse_incident(MAINTENANCE, "db-upgrade", {"web", "app"}, "maintenance")]
        out = build(sample_data(), manual, T0.replace(month=11, day=2, hour=2, minute=30))
        index = (out / "en" / "index.html").read_text(encoding="utf-8")
        self.assertIn("mw mw-in-progress", index)
        self.assertIn("banner-maintenance", index)

    def test_index_has_no_inline_history_but_links_to_it(self):
        index = (self.out / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("incident-summary", index)  # no inline past-incidents list
        self.assertNotIn("<details", index)
        self.assertIn('class="history-button" href="history.html"', index)
        self.assertIn("App login fails", (self.out / "history.html").read_text(encoding="utf-8"))

    def test_index_links_the_feed_near_the_top(self):
        index = (self.out / "index.html").read_text(encoding="utf-8")
        main = index.split('<main id="main"', 1)[1]
        link = '<a class="feed-link" href="feed.xml"'
        self.assertIn(link, main)
        self.assertLess(main.index(link), main.index("banner banner-"))
        self.assertIn("Subscribe (Atom)</span>", main)
        self.assertNotIn("status.json", index)

    def test_incident_pages_link_the_feed_near_the_top(self):
        for path in (self.out / "incidents").glob("*.html"):
            main = path.read_text(encoding="utf-8").split('<main id="main"', 1)[1]
            self.assertIn('<a class="feed-link" href="../feed.xml"', main, path.name)
            self.assertLess(main.index("feed-link"), main.index("<h1"), path.name)

    def test_feed_has_automatic_incidents_and_labelled_maintenance(self):
        feed = (self.out / "feed.xml").read_text(encoding="utf-8")
        entries = feed.split("<entry>")[1:]
        auto = [e for e in entries if "/incidents/auto-web-" in e]
        self.assertEqual(len(auto), 1)
        self.assertIn("Web is unavailable", auto[0])
        self.assertIn('<category term="incident"', auto[0])
        maintenance = [e for e in entries if "/incidents/db-upgrade.html" in e]
        self.assertEqual(len(maintenance), 1)
        self.assertIn("<title>Maintenance: ", maintenance[0])
        self.assertIn('<category term="maintenance" label="Maintenance"/>', maintenance[0])
        self.assertIn("Scheduled", maintenance[0])
        manual = [e for e in entries if "2026-10-30-login" in e][0]
        self.assertNotIn("Maintenance: ", manual)

    def test_day_bar_shows_incident_colour(self):
        index = (self.out / "index.html").read_text(encoding="utf-8")
        today = re.findall(r'<li class="bar b-(\w+)[^"]*"[^>]*aria-label="Oct 30, 2026[^"]*"', index)
        # web had 60 % uptime (critical), app 100 % but a major manual incident, soon is not live.
        self.assertEqual(today, ["critical", "major", "none"])

    def test_banner_and_active_incident(self):
        manual = [parse_incident(INCIDENT.replace("## resolved 2026-10-30 18:40\nFixed.\n", ""), "open", {"web", "app"})]
        out = build(sample_data(), manual, self.now)
        index = (out / "en" / "index.html").read_text(encoding="utf-8")
        self.assertIn("Some services are partly unavailable", index)
        self.assertIn('class="incident-card impact-major"', index)
        self.assertIn("<strong>looking</strong>", index)

    def test_escaping(self):
        manual = [parse_incident(INCIDENT.replace("App login fails", "<script>x</script>"), "xss", {"web", "app"})]
        out = build(sample_data(), manual, self.now)
        for path in out.rglob("*.html"):
            self.assertNotIn("<script>x", path.read_text(encoding="utf-8"))

    def test_auto_incident_has_a_page_and_feed_entry(self):
        feed = (self.out / "feed.xml").read_text(encoding="utf-8")
        auto = list((self.out / "incidents").glob("auto-web-*.html"))
        self.assertEqual(len(auto), 1)
        self.assertIn(auto[0].name, feed)
        self.assertIn("Web is unavailable", auto[0].read_text(encoding="utf-8"))

    def test_uptime_formatting(self):
        self.assertEqual(render.fmt_uptime(100.0, "en"), "100.0 % uptime")
        self.assertEqual(render.fmt_uptime(99.996, "en"), "99.99 % uptime")
        self.assertEqual(render.fmt_uptime(None, "en"), "No data yet")
        self.assertEqual(render.fmt_uptime(99.996, "de"), "99,99 % Verfügbarkeit")


if __name__ == "__main__":
    unittest.main()
