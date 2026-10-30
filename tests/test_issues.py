import json
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from statuspage import render
from statuspage.config import all_components, issue_labels, load_config
from statuspage.issues import items_from_issues, parse_body, parse_time, split_keyword, INCIDENT_KEYWORDS
from statuspage.manual import maintenance_state, phase
from statuspage.model import empty_data

ROOT = Path(__file__).resolve().parent.parent
CONFIG = load_config(ROOT / "config.json")
IDS = {c["id"] for c in all_components(CONFIG)}
ISSUES = json.loads((ROOT / "tests" / "fixtures" / "issues.json").read_text(encoding="utf-8"))


def items():
    return {i["id"]: i for i in items_from_issues(ISSUES, {"lukislp"}, IDS)}


class MappingTest(unittest.TestCase):
    def test_only_owner_issues_with_a_label_count(self):
        # #2 outsider, #4 withdrawn, #6 pull request, #7 invalid maintenance are dropped.
        self.assertEqual(sorted(items()), ["issue-1", "issue-3", "issue-5", "issue-8"])

    def test_outsider_comments_are_ignored(self):
        incident = items()["issue-1"]
        self.assertNotIn("haha", json.dumps(incident))

    def test_incident_from_issue_form(self):
        incident = items()["issue-1"]
        self.assertEqual(incident["impact"], "major")
        self.assertEqual(incident["components"], ["studylife-demo"])
        self.assertEqual(
            [(u["status"], u["at"]) for u in incident["updates"]],
            [
                ("investigating", "2026-10-28T09:12:00Z"),
                ("identified", "2026-10-28T09:30:00Z"),
                ("resolved", "2026-10-28T10:04:00Z"),
            ],
        )
        self.assertEqual(incident["updates"][1]["body"], "The nightly demo reset left the demo user without a password.")
        self.assertEqual(incident["updates"][2]["body"], "The demo seed was restored.")
        # The owner's resolved comment came before closing, so it is the resolution time.
        self.assertEqual(incident["resolved_at"], "2026-10-28T10:04:00Z")

    def test_closing_without_comment_resolves_at_closed_at(self):
        incident = items()["issue-5"]
        self.assertEqual(incident["resolved_at"], "2026-10-27T10:30:00Z")
        self.assertEqual(incident["updates"][-1]["status"], "resolved")
        self.assertEqual(incident["components"], ["portfolio"])  # unknown label component dropped
        self.assertEqual(incident["impact"], "minor")  # default

    def test_free_text_body_fields(self):
        incident = items()["issue-8"]
        self.assertEqual(incident["impact"], "critical")
        self.assertEqual(incident["components"], ["piwatch-demo"])
        self.assertEqual(incident["updates"][0]["body"], "Pages take several seconds to load.")
        self.assertEqual(incident["updates"][-1]["status"], "monitoring")
        self.assertIsNone(incident["resolved_at"])
        self.assertEqual(phase(incident, "2026-10-30T19:00:00Z"), "active")

    def test_impact_label_wins_over_form(self):
        issue = dict(ISSUES[0], labels=[{"name": "incident"}, {"name": "impact:critical"}])
        self.assertEqual(items_from_issues([issue], {"lukislp"}, IDS)[0]["impact"], "critical")

    def test_maintenance_window_with_offset(self):
        maint = items()["issue-3"]
        self.assertEqual(maint["impact"], "maintenance")
        self.assertEqual(maint["scheduled_start"], "2026-11-02T19:00:00Z")
        self.assertEqual(maint["scheduled_end"], "2026-11-02T20:00:00Z")
        self.assertEqual(maint["components"], ["homelabhub-demo", "piwatch-demo"])
        self.assertEqual(maintenance_state(maint, "2026-11-01T00:00:00Z"), "scheduled")
        self.assertEqual(maintenance_state(maint, "2026-11-02T19:30:00Z"), "in-progress")
        self.assertEqual(maintenance_state(maint, "2026-11-02T20:30:00Z"), "completed")

    def test_closed_maintenance_is_completed_early(self):
        issue = dict(ISSUES[2], state="closed", state_reason="completed", closed_at="2026-11-02T19:20:00Z")
        maint = items_from_issues([issue], {"lukislp"}, IDS)[0]
        self.assertEqual(maintenance_state(maint, "2026-11-02T19:30:00Z"), "completed")

    def test_cancelled_maintenance_disappears(self):
        issue = dict(ISSUES[2], state="closed", state_reason="not_planned", closed_at="2026-11-01T10:00:00Z")
        self.assertEqual(items_from_issues([issue], {"lukislp"}, IDS), [])

    def test_unlabelled_owner_issue_is_ignored(self):
        issue = dict(ISSUES[0], labels=[])
        self.assertEqual(items_from_issues([issue], {"lukislp"}, IDS), [])


class ParsingTest(unittest.TestCase):
    def test_parse_time_variants(self):
        self.assertEqual(parse_time("2026-11-02T20:00+01:00"), "2026-11-02T19:00:00Z")
        self.assertEqual(parse_time("2026-11-02 19:00"), "2026-11-02T19:00:00Z")
        self.assertEqual(parse_time("2026-11-02T19:00Z"), "2026-11-02T19:00:00Z")
        self.assertIsNone(parse_time("tomorrow"))

    def test_keywords(self):
        self.assertEqual(split_keyword("Monitoring: ok", INCIDENT_KEYWORDS, "update"), ("monitoring", "Ok"))
        self.assertEqual(split_keyword("**Resolved** - done", INCIDENT_KEYWORDS, "update"), ("resolved", "Done"))
        self.assertEqual(split_keyword("Identifiedness is key", INCIDENT_KEYWORDS, "update")[0], "update")
        self.assertEqual(split_keyword("just text", INCIDENT_KEYWORDS, "update"), ("update", "just text"))

    def test_html_comments_are_stripped(self):
        _, text, _ = parse_body("visible <!-- secret -->")
        self.assertEqual(text, "visible")

    def test_no_response_sections(self):
        fields, text, checked = parse_body("### Impact\n\n_No response_\n\n### What is happening?\n\nx")
        self.assertEqual(fields["impact"], "")
        self.assertEqual(text, "x")
        self.assertEqual(checked, set())


class RenderingTest(unittest.TestCase):
    def test_untrusted_text_is_escaped(self):
        out = Path(tempfile.mkdtemp()) / "_site"
        now = datetime(2026, 10, 30, 19, 0, tzinfo=timezone.utc)
        render.build_site(CONFIG, empty_data(), list(items().values()), now, ROOT / "site", out)
        for path in out.rglob("*.html"):
            html = path.read_text(encoding="utf-8")
            self.assertNotIn("<script>alert", html)
            self.assertNotIn("<img src=x", html)
            self.assertNotIn('href="javascript:', html)
        page = (out / "incidents" / "issue-5.html").read_text(encoding="utf-8")
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotIn("hidden note", page)
        index = (out / "index.html").read_text(encoding="utf-8")
        self.assertIn("PiWatch demo is slow", index)  # active incident card
        self.assertIn("Demo host kernel update", index)  # maintenance section


class TemplatesTest(unittest.TestCase):
    def test_issue_forms_list_every_component(self):
        for name in ("incident.yml", "maintenance.yml"):
            text = (ROOT / ".github" / "ISSUE_TEMPLATE" / name).read_text(encoding="utf-8")
            self.assertEqual(set(re.findall(r"\(`([a-z0-9-]+)`\)", text)), IDS, name)

    def test_labels_cover_components(self):
        names = {n for n, _ in issue_labels(CONFIG)}
        self.assertTrue({f"component:{c}" for c in IDS} <= names)
        self.assertTrue({"incident", "maintenance"} <= names)


if __name__ == "__main__":
    unittest.main()
