"""Hidden components: probed and alerted on, but no trace in anything public."""

import contextlib
import io
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from statuspage import __main__ as cli
from statuspage import model, notify, render
from statuspage.config import ConfigError, hidden_config, load_config, public_config, validate_config

from .test_model import T0

ROOT = Path(__file__).resolve().parent.parent
SECRET_ID = "review-box"
SECRET_NAME = "Quiet Review Instance"
SECRET_HOST = "quiet-review.example.net"
SECRET_URL = f"https://{SECRET_HOST}/api/auth/methods"
PUBLIC_URL = "https://a.example.org"
RAW = {
    "site": {"title": "test status", "base_url": "https://status.example.org"},
    "groups": [
        {"name": "Demos", "components": [{"id": "web", "name": "Web", "checks": [{"url": PUBLIC_URL}]}]},
        {
            "name": "Private",
            "components": [
                {
                    "id": SECRET_ID,
                    "name": SECRET_NAME,
                    "hidden": True,
                    "checks": [{"url": SECRET_URL, "expect_body_contains": '"passwordSignIn":true'}],
                }
            ],
        },
    ],
}
GOOD = b'{"passwordSignIn":true}'


def reply(body=b"ok", status=200):
    return {"status": status, "body": body, "headers": {}, "latency_ms": 90, "tls_days_left": 60, "error": None}


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


class Harness:
    """Runs `python -m statuspage check` against a temporary repository copy, with a scripted
    network and a recording Telegram notifier."""

    def __init__(self, raw=RAW):
        self.dir = Path(tempfile.mkdtemp())
        self.config = self.dir / "config.json"
        self.config.write_text(json.dumps(raw), encoding="utf-8")
        self.data = self.dir / "data"
        self.hidden = self.dir / "hidden"
        self.notifier = FakeNotifier()
        self.log = ""
        self.minute = 0
        self.hidden_body = GOOD
        self.hidden_status = 200

    def probe_all(self, checks, timeout, retry_delay, headers=None):
        return [
            reply(self.hidden_body, self.hidden_status) if c["url"] == SECRET_URL else reply() for c in checks
        ]

    def run(self):
        out = io.StringIO()
        now = T0 + timedelta(minutes=self.minute)
        self.minute += 5
        clock = mock.Mock(wraps=cli.datetime)
        clock.now.return_value = now
        with contextlib.redirect_stdout(out), mock.patch.object(cli, "probe_all", self.probe_all), mock.patch.object(
            cli, "datetime", clock
        ), mock.patch.object(notify, "from_env", return_value=self.notifier):
            code = cli.main(
                ["--config", str(self.config), "--data", str(self.data), "check", "--hidden-state", str(self.hidden)]
            )
        self.assertion_code = code
        self.log += out.getvalue()
        return out.getvalue()

    def break_it(self, runs=2):
        self.hidden_body, self.hidden_status = b"", 503
        for _ in range(runs):
            self.run()

    def repair(self, runs=2):
        self.hidden_body, self.hidden_status = GOOD, 200
        for _ in range(runs):
            self.run()

    def public_files(self):
        """Every file under data/ and the built site, as text."""
        site = self.dir / "_site"
        config = load_config(self.config)
        data = cli.read_data(self.data)
        render.build_site(config, data, [], T0 + timedelta(hours=1), ROOT / "site", site)
        return {p: p.read_text(encoding="utf-8") for d in (self.data, site) for p in d.rglob("*") if p.is_file()}


class HiddenConfigTest(unittest.TestCase):
    def test_hidden_must_be_a_bool(self):
        raw = json.loads(json.dumps(RAW))
        raw["groups"][1]["components"][0]["hidden"] = "yes"
        with self.assertRaises(ConfigError):
            validate_config(raw)

    def test_hidden_cannot_be_planned(self):
        raw = json.loads(json.dumps(RAW))
        raw["groups"][1]["components"][0]["planned"] = True
        with self.assertRaises(ConfigError):
            validate_config(raw)

    def test_split_drops_empty_groups(self):
        config = validate_config(RAW)
        self.assertEqual([g["name"] for g in public_config(config)["groups"]], ["Demos"])
        self.assertEqual([g["name"] for g in hidden_config(config)["groups"]], ["Private"])

    def test_the_real_config_declares_the_review_instance_hidden(self):
        config = load_config(ROOT / "config.json")
        (comp,) = [c for g in hidden_config(config)["groups"] for c in g["components"]]
        self.assertEqual(comp["id"], "studyatrium-review")
        self.assertEqual(comp["checks"][0]["expect_body_contains"], '"passwordSignIn":true')


class HiddenNoTraceTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.h.run()
        self.h.break_it()  # two failed runs: an automatic incident opens for the hidden component
        self.h.repair()

    def test_run_succeeded(self):
        self.assertEqual(self.h.assertion_code, 0)

    def test_committed_data_never_mentions_it(self):
        for name in ("status.json", "history.json", "incidents.json"):
            text = (self.h.data / name).read_text(encoding="utf-8")
            for secret in (SECRET_ID, SECRET_NAME, SECRET_HOST, "passwordSignIn"):
                self.assertNotIn(secret, text, name)
        self.assertEqual(json.loads((self.h.data / "incidents.json").read_text())["incidents"], [])

    def test_site_never_mentions_it_in_any_language_or_feed(self):
        files = self.h.public_files()
        names = {p.name for p in files}
        self.assertTrue({"index.html", "history.html", "feed.xml", "status.json"} <= names)
        for path, text in files.items():
            for secret in (SECRET_ID, SECRET_NAME, SECRET_HOST):
                self.assertNotIn(secret, text, str(path))
        self.assertTrue(any("/en/" in p.as_posix() for p in files))  # both languages were built

    def test_workflow_log_never_mentions_it(self):
        for secret in (SECRET_ID, SECRET_NAME, SECRET_HOST, "passwordSignIn"):
            self.assertNotIn(secret, self.h.log)
        self.assertIn("hidden checks: 1 ok, 0 failing", self.h.log)
        self.assertIn("hidden checks: 0 ok, 1 failing", self.h.log)

    def test_state_lives_in_the_private_directory(self):
        data = cli.read_data(self.h.hidden)
        self.assertIn(SECRET_ID, data["status"]["components"])
        (incident,) = data["incidents"]["incidents"]
        self.assertTrue(incident["resolved_at"])

    def test_overall_status_and_banner_ignore_it(self):
        with_hidden = Harness()
        with_hidden.run()
        with_hidden.break_it(3)  # hidden component is down right now
        plain_raw = json.loads(json.dumps(RAW))
        del plain_raw["groups"][1]
        plain = Harness(plain_raw)
        for _ in range(4):
            plain.run()
        a, b = with_hidden.public_files(), plain.public_files()
        site_a = {p.relative_to(with_hidden.dir / "_site").as_posix(): t for p, t in a.items() if "_site" in p.parts}
        site_b = {p.relative_to(plain.dir / "_site").as_posix(): t for p, t in b.items() if "_site" in p.parts}
        self.assertEqual(site_a["index.html"], site_b["index.html"])
        self.assertEqual(site_a["en/index.html"], site_b["en/index.html"])
        status_a, status_b = json.loads(site_a["status.json"]), json.loads(site_b["status.json"])
        self.assertEqual(status_a["status"], status_b["status"])
        self.assertEqual(status_a["status"]["indicator"], "operational")


class HiddenAlertsTest(unittest.TestCase):
    def test_down_and_recovery_alert_exactly_once_and_may_name_it(self):
        h = Harness()
        h.run()
        h.break_it(2)
        self.assertEqual(len(h.notifier.sent), 1)
        down = h.notifier.sent[0]
        self.assertIn(SECRET_NAME, down)
        self.assertTrue(down.startswith("🔴 Down:"))
        self.assertNotIn("/incidents/", down)  # no link to a page that does not exist
        h.break_it(3)  # still down: no repeat
        self.assertEqual(len(h.notifier.sent), 1)
        h.repair(3)
        self.assertEqual(len(h.notifier.sent), 2)
        self.assertTrue(h.notifier.sent[1].startswith(f"🟢 Recovered: {SECRET_NAME}"))
        h.repair(2)
        self.assertEqual(len(h.notifier.sent), 2)

    def test_state_is_what_prevents_a_duplicate(self):
        h = Harness()
        h.run()
        h.break_it(2)
        self.assertEqual(len(h.notifier.sent), 1)
        for path in h.hidden.iterdir():  # cache evicted: the state is gone
            path.unlink()
        h.break_it(2)
        self.assertEqual(len(h.notifier.sent), 2)  # at worst one duplicate alert

    def test_public_component_alerts_are_unchanged(self):
        h = Harness()
        h.run()
        notifier_text = notify.pending_alerts(model.empty_data(), {}, "https://status.example.org")
        self.assertEqual(notifier_text, [])
        self.assertEqual(h.notifier.sent, [])

    def test_a_hidden_error_leaves_the_public_run_intact(self):
        h = Harness()
        with mock.patch.object(cli, "run_hidden", side_effect=RuntimeError(SECRET_URL)):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), mock.patch.object(cli, "probe_all", h.probe_all):
                code = cli.main(["--config", str(h.config), "--data", str(h.data), "check", "--hidden-state", str(h.hidden)])
        self.assertEqual(code, 0)
        self.assertNotIn(SECRET_HOST, out.getvalue())
        self.assertIn("publish:", out.getvalue())


class AlertLinkTest(unittest.TestCase):
    def test_links_are_optional(self):
        from .test_notify import NAMES, BASE, outage

        (with_link,) = notify.pending_alerts(outage(), NAMES, BASE)
        (without,) = notify.pending_alerts(outage(), NAMES, BASE, links=False)
        self.assertIn("/en/incidents/", with_link.text)
        self.assertNotIn("/en/incidents/", without.text)


if __name__ == "__main__":
    unittest.main()
