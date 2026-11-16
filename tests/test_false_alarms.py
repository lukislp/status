import copy
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from statuspage import model, notify, render
from statuspage.config import ConfigError, validate_config

from .test_model import T0, config, fail, ok

ROOT = Path(__file__).resolve().parent.parent
NOTE = {"note": "The check was wrong.", "note_de": "Die Prüfung war falsch."}
NAMES = {"web": "Web"}


def run(data, minute, cfg, results=None):
    return model.apply_run(data, cfg, results or {"web": [ok()]}, T0 + timedelta(minutes=minute))


def resolved_outage(alerted=False):
    """web fails twice, then recovers twice: one resolved automatic incident."""
    cfg = config()
    data = model.empty_data()
    for minute, result in enumerate([ok(), fail(), fail(), ok(), ok()]):
        data, _ = run(data, minute * 5, cfg, {"web": [result]})
    (incident,) = data["incidents"]["incidents"]
    assert incident["resolved_at"]
    if alerted:
        incident["alerted"] = True
    return data, incident["id"]


def with_false_alarm(incident_id, **extra):
    return dict(config(), false_alarms=[{"incident": incident_id, **extra}])


class ApplyFalseAlarmTest(unittest.TestCase):
    def test_becomes_a_check_error_notice(self):
        data, iid = resolved_outage()
        data, events = run(data, 40, with_false_alarm(iid, **NOTE))
        (incident,) = data["incidents"]["incidents"]
        self.assertEqual(events["false_alarms"], [iid])
        self.assertEqual(incident["impact"], "none")
        self.assertTrue(incident["false_alarm"])
        self.assertEqual(incident["title"], "False alarm: check error for Web")
        self.assertEqual(incident["updates"][-1]["status"], "false_alarm")
        self.assertEqual(incident["updates"][-1]["body"], "The check was wrong.")
        self.assertNotIn("affected for about", " ".join(u["body"] for u in incident["updates"]))

    def test_applied_once_and_idempotent(self):
        data, iid = resolved_outage()
        cfg = with_false_alarm(iid, **NOTE)
        once, _ = run(data, 40, cfg)
        twice, events = run(once, 45, cfg)
        self.assertEqual(events["false_alarms"], [])
        self.assertEqual(twice["incidents"], once["incidents"])

    def test_survives_a_cache_copy(self):
        # The checker may continue from the older repository copy or from a corrected cache copy.
        data, iid = resolved_outage()
        cfg = with_false_alarm(iid, **NOTE)
        corrected, _ = run(data, 40, cfg)
        from_cache, e1 = run(copy.deepcopy(corrected), 45, cfg)
        from_repo, e2 = run(copy.deepcopy(data), 45, cfg)
        self.assertEqual(e1["false_alarms"], [])
        self.assertEqual(e2["false_alarms"], [iid])
        self.assertEqual(from_cache["incidents"]["incidents"][0]["impact"], "none")
        self.assertEqual(from_repo["incidents"]["incidents"][0]["impact"], "none")

    def test_unknown_id_is_ignored_and_reported(self):
        data, _ = resolved_outage()
        before = copy.deepcopy(data["incidents"])
        data, events = run(data, 40, with_false_alarm("auto-web-20200101-0000"))
        self.assertEqual(events["unknown_false_alarms"], ["auto-web-20200101-0000"])
        self.assertEqual(data["incidents"], before)

    def test_open_incident_waits_for_its_resolution(self):
        cfg = config()
        data = model.empty_data()
        for minute, result in enumerate([ok(), fail(), fail()]):
            data, _ = run(data, minute * 5, cfg, {"web": [result]})
        iid = data["incidents"]["incidents"][0]["id"]
        data, events = run(data, 20, with_false_alarm(iid), {"web": [fail()]})
        self.assertEqual(events["false_alarms"], [])
        self.assertEqual(events["unknown_false_alarms"], [])
        self.assertNotEqual(data["incidents"]["incidents"][0]["impact"], "none")

    def test_standard_text_without_a_note(self):
        data, iid = resolved_outage()
        data, _ = run(data, 40, with_false_alarm(iid))
        update = data["incidents"]["incidents"][0]["updates"][-1]
        self.assertIn("available throughout", update["body"])

    def test_correction_is_published(self):
        events = {"changed": [], "opened": [], "resolved": [], "false_alarms": ["x"]}
        self.assertTrue(model.should_publish(events, model.iso(T0), T0, 30))


class NoAlertTest(unittest.TestCase):
    def test_correction_sends_no_alert(self):
        data, iid = resolved_outage(alerted=True)  # "down" was delivered, "recovered" was not
        self.assertEqual(len(notify.pending_alerts(data, NAMES, "https://x")), 1)
        data, _ = run(data, 40, with_false_alarm(iid, **NOTE))
        self.assertEqual(notify.pending_alerts(data, NAMES, "https://x"), [])


class RenderFalseAlarmTest(unittest.TestCase):
    def setUp(self):
        data, self.iid = resolved_outage()
        self.now = T0 + timedelta(hours=1)
        data, _ = model.apply_run(data, with_false_alarm(self.iid, **NOTE), {"web": [ok()]}, self.now)
        self.out = Path(tempfile.mkdtemp()) / "_site"
        render.build_site(config(), data, [], self.now, ROOT / "site", self.out)

    def read(self, rel):
        return (self.out / rel).read_text(encoding="utf-8")

    def test_incident_page_in_both_languages(self):
        en = self.read(f"en/incidents/{self.iid}.html")
        de = self.read(f"incidents/{self.iid}.html")
        self.assertIn("False alarm: check error for Web", en)
        self.assertIn("The check was wrong.", en)
        self.assertIn("Fehlalarm: Prüffehler bei Web", de)
        self.assertIn("Die Prüfung war falsch.", de)
        for page in (en, de):
            self.assertNotIn("affected for about", page)
            self.assertNotIn("beeinträchtigt", page)
            self.assertNotIn('class="affects"', page)
            self.assertIn("impact-none", page)

    def test_feed_uses_the_new_title(self):
        feed = self.read("en/feed.xml")
        self.assertIn("False alarm: check error for Web", feed)
        self.assertNotIn("Web is unavailable", feed)

    def test_no_outage_on_the_page(self):
        self.assertIn('"incidents": []', self.read("status.json"))
        self.assertNotIn("impact-critical", self.read("en/index.html"))


class ConfigTest(unittest.TestCase):
    def raw(self, entries):
        return {
            "site": {"title": "t", "base_url": "https://x"},
            "false_alarms": entries,
            "groups": [{"name": "g", "components": [{"id": "a", "name": "A", "checks": [{"url": "https://x"}]}]}],
        }

    def test_valid_entry(self):
        cfg = validate_config(self.raw([{"incident": "auto-a-20261116-0450", "note": "n", "note_de": "d"}]))
        self.assertEqual(cfg["false_alarms"][0]["incident"], "auto-a-20261116-0450")

    def test_default_is_empty(self):
        raw = self.raw([])
        del raw["false_alarms"]
        self.assertEqual(validate_config(raw)["false_alarms"], [])

    def test_rejects_bad_entries(self):
        bad_entries = [
            [{"incident": "web-1"}],
            [{"incident": "auto-a-20261116-0450", "x": 1}],
            [{"note": "n"}],
            [{"incident": "auto-a-20261116-0450", "note": ""}],
            "auto-a-20261116-0450",
        ]
        for bad in bad_entries:
            with self.assertRaises(ConfigError, msg=bad):
                validate_config(self.raw(bad))


if __name__ == "__main__":
    unittest.main()
