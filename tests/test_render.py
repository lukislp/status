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
        self.out = build(sample_data(), manual, self.now)

    def test_expected_files(self):
        for name in ["index.html", "history.html", "404.html", "status.json", "feed.xml", "CNAME",
                     "style.css", "bars.js", "favicon.svg", "incidents/2026-10-30-login.html",
                     "incidents/db-upgrade.html"]:
            self.assertTrue((self.out / name).is_file(), name)
        self.assertEqual((self.out / "CNAME").read_text().strip(), "status.example.org")

    def test_html_is_balanced(self):
        for path in self.out.rglob("*.html"):
            parser = TagBalance()
            parser.feed(path.read_text(encoding="utf-8"))
            self.assertEqual(parser.errors, [], path.name)
            self.assertEqual(parser.stack, [], path.name)

    def test_no_third_party_requests(self):
        pattern = re.compile(r"""(?:src|href)\s*=\s*["'](?:https?:)?//|url\(\s*["']?(?:https?:)?//|@import""")
        for path in self.out.rglob("*"):
            if path.suffix not in (".html", ".css", ".js"):
                continue
            text = path.read_text(encoding="utf-8")
            # Plain navigation links (<a href>) are fine: they are not loaded by the page.
            text = re.sub(r"<a\s[^>]*>", "", text)
            text = re.sub(r'<link rel="alternate"[^>]*>', "", text)
            self.assertIsNone(pattern.search(text), path.name)

    def test_ninety_bars_per_component(self):
        index = (self.out / "index.html").read_text(encoding="utf-8")
        self.assertEqual(index.count('class="bar '), 90 * 3)
        self.assertEqual(index.count('tabindex="0" aria-label='), 3)  # one tab stop per component

    def test_status_reflects_manual_incident_and_planned(self):
        doc = json.loads((self.out / "status.json").read_text(encoding="utf-8"))
        by_id = {c["id"]: c for c in doc["components"]}
        self.assertEqual(by_id["soon"]["status"], "planned")
        self.assertIsNone(by_id["soon"]["uptime_90d"])
        self.assertEqual(by_id["web"]["status"], "operational")
        self.assertAlmostEqual(by_id["web"]["uptime_90d"], 60.0)
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
        self.assertNotIn('class="maintenance"', (out / "index.html").read_text(encoding="utf-8"))

    def test_running_maintenance_sets_banner(self):
        manual = [parse_incident(MAINTENANCE, "db-upgrade", {"web", "app"}, "maintenance")]
        out = build(sample_data(), manual, T0.replace(month=11, day=2, hour=2, minute=30))
        index = (out / "index.html").read_text(encoding="utf-8")
        self.assertIn("mw mw-in-progress", index)
        self.assertIn("banner-maintenance", index)

    def test_index_has_no_inline_history_but_links_to_it(self):
        index = (self.out / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("incident-summary", index)  # no inline past-incidents list
        self.assertNotIn("<details", index)
        self.assertIn('class="history-button" href="history.html"', index)
        self.assertIn("App login fails", (self.out / "history.html").read_text(encoding="utf-8"))

    def test_no_feed_or_json_buttons(self):
        index = (self.out / "index.html").read_text(encoding="utf-8")
        self.assertNotIn('href="feed.xml">', index.split("</head>")[1])
        self.assertNotIn("status.json", index)
        self.assertNotIn("Subscribe", index)

    def test_day_bar_shows_incident_colour(self):
        index = (self.out / "index.html").read_text(encoding="utf-8")
        today = re.findall(r'<li class="bar b-(\w+)[^"]*"[^>]*aria-label="Oct 30, 2026[^"]*"', index)
        # web had 60 % uptime (critical), app 100 % but a major manual incident, soon is not live.
        self.assertEqual(today, ["critical", "major", "none"])

    def test_banner_and_active_incident(self):
        manual = [parse_incident(INCIDENT.replace("## resolved 2026-10-30 18:40\nFixed.\n", ""), "open", {"web", "app"})]
        out = build(sample_data(), manual, self.now)
        index = (out / "index.html").read_text(encoding="utf-8")
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
        self.assertEqual(render.fmt_uptime(100.0), "100.0 % uptime")
        self.assertEqual(render.fmt_uptime(99.996), "99.99 % uptime")
        self.assertEqual(render.fmt_uptime(None), "No data yet")


if __name__ == "__main__":
    unittest.main()
