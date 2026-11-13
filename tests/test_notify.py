import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import timedelta

from statuspage import __main__ as cli
from statuspage import model, notify

from .test_model import T0, config, fail, ok

TOKEN = "123456:SECRET-token"
NAMES = {"web": "Web", "app": "App"}
BASE = "https://status.example.org"
ENV = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42"}


class FakeNotifier:
    def __init__(self, failing=False):
        self.failing = failing
        self.sent = []

    def send(self, text):
        if self.failing:
            raise notify.NotifyError("down")
        self.sent.append(text)


def run(data, results, minute):
    data, _ = model.apply_run(data, config(), results, T0 + timedelta(minutes=minute))
    return data


def outage():
    """web fails twice (threshold 2): one automatic incident is open after minute 10."""
    data = run(model.empty_data(), {"web": [ok()]}, 0)
    data = run(data, {"web": [fail("HTTP 503")]}, 5)
    return run(data, {"web": [fail("HTTP 503")]}, 10)


def partial_outage():
    data = model.empty_data()
    for minute in (0, 5):
        data = run(data, {"app": [ok(), fail()]}, minute)
    return data


class PendingAlertsTest(unittest.TestCase):
    def test_nothing_before_the_threshold(self):
        data = run(run(model.empty_data(), {"web": [ok()]}, 0), {"web": [fail()]}, 5)
        self.assertEqual(notify.pending_alerts(data, NAMES, BASE), [])

    def test_slow_component_gives_nothing(self):
        data = run(model.empty_data(), {"web": [ok(latency=9000)]}, 0)
        self.assertEqual(notify.pending_alerts(data, NAMES, BASE), [])

    def test_open_incident_gives_one_down_alert(self):
        data = outage()
        (alert,) = notify.pending_alerts(data, NAMES, BASE)
        self.assertEqual(alert.kind, notify.OPENED)
        self.assertTrue(alert.text.startswith("🔴 Down: Web – x: HTTP 503 – since 12:05 UTC\n"))
        self.assertTrue(alert.text.endswith(f"{BASE}/en/incidents/{alert.incident_id}.html"))

    def test_partial_outage_is_labelled_partly_down(self):
        (alert,) = notify.pending_alerts(partial_outage(), NAMES, BASE)
        self.assertIn("Partly down: App", alert.text)

    def test_still_open_after_delivery_gives_nothing(self):
        data = outage()
        notify.dispatch(data, FakeNotifier(), NAMES, BASE)
        data = run(data, {"web": [fail()]}, 15)
        self.assertEqual(notify.pending_alerts(data, NAMES, BASE), [])

    def test_resolved_gives_recovery_alert_once(self):
        data = outage()
        notify.dispatch(data, FakeNotifier(), NAMES, BASE)
        data = run(run(data, {"web": [ok()]}, 20), {"web": [ok()]}, 25)
        fake = FakeNotifier()
        sent, failed = notify.dispatch(data, fake, NAMES, BASE)
        self.assertEqual((len(sent), failed), (1, []))
        self.assertTrue(fake.sent[0].startswith("🟢 Recovered: Web after 20 min\n"))
        self.assertEqual(notify.dispatch(data, fake, NAMES, BASE), ([], []))

    def test_escalation_to_down_sends_one_update(self):
        data = partial_outage()
        notify.dispatch(data, FakeNotifier(), NAMES, BASE)
        data = run(run(data, {"app": [fail(), fail()]}, 10), {"app": [fail(), fail()]}, 15)
        fake = FakeNotifier()
        sent, _ = notify.dispatch(data, fake, NAMES, BASE)
        self.assertEqual([a.kind for a in sent], [notify.ESCALATED])
        self.assertIn("now Down: App", fake.sent[0])
        data = run(data, {"app": [fail(), fail()]}, 20)
        self.assertEqual(notify.dispatch(data, fake, NAMES, BASE), ([], []))

    def test_unannounced_incident_gets_no_recovery_alert(self):
        data = run(run(outage(), {"web": [ok()]}, 20), {"web": [ok()]}, 25)
        self.assertEqual(notify.pending_alerts(data, NAMES, BASE), [])

    def test_manual_incidents_are_ignored(self):
        data = model.empty_data()
        data["incidents"]["incidents"].append(
            {"id": "manual", "title": "x", "components": ["web"], "impact": "critical",
             "created_at": model.iso(T0), "resolved_at": None, "updates": []}
        )
        self.assertEqual(notify.pending_alerts(data, NAMES, BASE), [])

    def test_only_the_alerted_incident_changes(self):
        data = outage()
        status_before = json.dumps(data["status"], sort_keys=True)
        notify.dispatch(data, FakeNotifier(), NAMES, BASE)
        self.assertEqual(json.dumps(data["status"], sort_keys=True), status_before)
        incident = data["incidents"]["incidents"][0]
        self.assertTrue(incident["alerted"])
        self.assertNotIn("recovery_alerted", incident)


class DispatchTest(unittest.TestCase):
    def test_failed_send_is_retried_then_not_repeated(self):
        data = outage()
        sent, failed = notify.dispatch(data, FakeNotifier(failing=True), NAMES, BASE)
        self.assertEqual((sent, len(failed)), ([], 1))
        self.assertNotIn("alerted", data["incidents"]["incidents"][0])
        fake = FakeNotifier()
        sent, failed = notify.dispatch(data, fake, NAMES, BASE)
        self.assertEqual((len(sent), failed), (1, []))
        self.assertEqual(notify.dispatch(data, fake, NAMES, BASE), ([], []))
        self.assertEqual(len(fake.sent), 1)


class TelegramTest(unittest.TestCase):
    def test_request_shape(self):
        calls = []

        def post(url, body):
            calls.append((url, json.loads(body)))
            return 200, '{"ok":true}'

        notify.TelegramNotifier(TOKEN, "42", post).send("hi")
        url, payload = calls[0]
        self.assertEqual(url, f"https://api.telegram.org/bot{TOKEN}/sendMessage")
        self.assertEqual(payload, {"chat_id": "42", "text": "hi", "disable_web_page_preview": True})

    def test_errors_never_contain_the_token(self):
        def leaking(url, body):
            raise OSError(f"cannot reach {url}")

        with self.assertRaises(notify.NotifyError) as ctx:
            notify.TelegramNotifier(TOKEN, "42", leaking).send("hi")
        self.assertNotIn(TOKEN, str(ctx.exception))

        def refused(url, body):
            return 401, json.dumps({"ok": False, "description": f"Unauthorized {TOKEN}"})

        with self.assertRaises(notify.NotifyError) as ctx:
            notify.TelegramNotifier(TOKEN, "42", refused).send("hi")
        self.assertIn("HTTP 401", str(ctx.exception))
        self.assertNotIn(TOKEN, str(ctx.exception))

    def test_every_request_targets_only_the_configured_chat(self):
        calls = []

        def post(url, body):
            calls.append((url, json.loads(body)))
            return 200, "{}"

        data = outage()
        cli.send_alerts(data, config(), ENV, post)
        data = run(run(data, {"web": [ok()]}, 20), {"web": [ok()]}, 25)
        cli.send_alerts(data, config(), ENV, post)
        notify.TelegramNotifier(TOKEN, "42", post).send("test")
        self.assertEqual(len(calls), 3)
        for url, payload in calls:
            self.assertEqual(url, f"https://api.telegram.org/bot{TOKEN}/sendMessage")
            self.assertEqual(payload["chat_id"], "42")

    def test_server_errors_are_reported_without_the_token_and_retried(self):
        data = outage()
        out = io.StringIO()
        with redirect_stdout(out):
            sent = cli.send_alerts(data, config(), ENV, lambda url, body: (502, "<html>Bad Gateway</html>"))
        self.assertEqual(sent, [])
        self.assertIn("::warning title=Telegram alert not delivered", out.getvalue())
        self.assertNotIn(TOKEN, out.getvalue())
        self.assertEqual(len(notify.pending_alerts(data, NAMES, BASE)), 1)

    def test_from_env_needs_both_values(self):
        self.assertIsNone(notify.from_env({"TELEGRAM_BOT_TOKEN": TOKEN}))
        self.assertIsNone(notify.from_env({"TELEGRAM_CHAT_ID": "42"}))
        self.assertIsNotNone(notify.from_env(ENV))


class SendAlertsTest(unittest.TestCase):
    def test_missing_secrets_warn_once_and_change_nothing(self):
        data = outage()
        before = json.dumps(data, sort_keys=True)
        out = io.StringIO()
        with redirect_stdout(out):
            sent = cli.send_alerts(data, config(), {})
        self.assertEqual(sent, [])
        self.assertEqual(out.getvalue().count("::warning"), 1)
        self.assertEqual(json.dumps(data, sort_keys=True), before)

    def test_failed_send_warns_without_the_token_and_keeps_data_clean(self):
        data = outage()

        def leaking(url, body):
            raise OSError(f"cannot reach {url}")

        out = io.StringIO()
        with redirect_stdout(out):
            sent = cli.send_alerts(data, config(), ENV, leaking)
        self.assertEqual(sent, [])
        self.assertIn("::warning title=Telegram alert not delivered", out.getvalue())
        self.assertNotIn(TOKEN, out.getvalue())
        self.assertNotIn("alerted", data["incidents"]["incidents"][0])

    def test_delivered_alert_is_stored_and_never_holds_the_token(self):
        data = outage()
        out = io.StringIO()
        with redirect_stdout(out):
            sent = cli.send_alerts(data, config(), ENV, lambda url, body: (200, "{}"))
        self.assertEqual(len(sent), 1)
        self.assertTrue(data["incidents"]["incidents"][0]["alerted"])
        self.assertNotIn(TOKEN, out.getvalue() + json.dumps(data))


if __name__ == "__main__":
    unittest.main()
