import unittest
from datetime import datetime, timedelta, timezone

from statuspage import model
from statuspage.config import ConfigError, validate_config

SETTINGS = {
    "timeout_seconds": 15,
    "retry_delay_seconds": 0,
    "failure_threshold": 2,
    "recovery_threshold": 2,
    "slow_ms": 5000,
    "tls_warn_days": 7,
    "retention_days": 90,
    "publish_interval_minutes": 30,
}
T0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def config():
    return validate_config(
        {
            "site": {"title": "test status", "base_url": "https://status.example.org"},
            "settings": SETTINGS,
            "groups": [
                {
                    "name": "Demos",
                    "components": [
                        {"id": "web", "name": "Web", "checks": [{"url": "https://a.example.org"}]},
                        {
                            "id": "app",
                            "name": "App",
                            "checks": [{"url": "https://b.example.org"}, {"url": "https://b.example.org/api"}],
                        },
                        {"id": "soon", "name": "Soon", "planned": True, "checks": [{"url": "https://c.example.org"}]},
                    ],
                }
            ],
        }
    )


def ok(latency=100):
    return {"name": "x", "ok": True, "slow": latency > 5000, "tls_warning": False, "latency_ms": latency,
            "http_status": 200, "tls_days_left": 60, "error": None}


def fail(error="HTTP 503"):
    return {"name": "x", "ok": False, "slow": False, "tls_warning": False, "latency_ms": None,
            "http_status": 503, "tls_days_left": 60, "error": error}


def response(status=200, body=b"", headers=None, latency=120, tls=60, error=None):
    return {"status": status, "body": body, "headers": headers or {}, "latency_ms": latency,
            "tls_days_left": tls, "error": error}


class EvaluateCheckTest(unittest.TestCase):
    def test_plain_200_is_ok(self):
        self.assertTrue(model.evaluate_check({"url": "u"}, response(), SETTINGS)["ok"])

    def test_unexpected_status_fails(self):
        result = model.evaluate_check({"url": "u"}, response(status=502), SETTINGS)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "HTTP 502")

    def test_expect_status_list(self):
        check = {"url": "u", "expect_status": [200, 204]}
        self.assertTrue(model.evaluate_check(check, response(status=204), SETTINGS)["ok"])

    def test_transport_error_fails(self):
        result = model.evaluate_check({"url": "u"}, response(status=None, error="DNS lookup failed"), SETTINGS)
        self.assertEqual(result["error"], "DNS lookup failed")

    def test_header_expectation_is_case_insensitive_on_name(self):
        check = {"url": "u", "expect_header": {"X-MowDeck-Demo": "1"}}
        self.assertTrue(model.evaluate_check(check, response(headers={"x-mowdeck-demo": "1"}), SETTINGS)["ok"])
        self.assertIn("missing header", model.evaluate_check(check, response(), SETTINGS)["error"])
        self.assertFalse(model.evaluate_check(check, response(headers={"x-mowdeck-demo": "0"}), SETTINGS)["ok"])

    def test_body_contains(self):
        check = {"url": "u", "expect_body_contains": "Körber"}
        self.assertTrue(model.evaluate_check(check, response(body="Lukas Körber".encode()), SETTINGS)["ok"])
        self.assertFalse(model.evaluate_check(check, response(body=b"Cloudflare error"), SETTINGS)["ok"])

    def test_json_expectation(self):
        check = {"url": "u", "expect_json": {"demo": True}}
        self.assertTrue(model.evaluate_check(check, response(body=b'{"demo":true}'), SETTINGS)["ok"])
        self.assertFalse(model.evaluate_check(check, response(body=b'{"demo":false}'), SETTINGS)["ok"])
        self.assertEqual(model.evaluate_check(check, response(body=b"<html>"), SETTINGS)["error"],
                         "response is not valid JSON")
        self.assertFalse(model.evaluate_check(check, response(body=b"[1]"), SETTINGS)["ok"])

    def test_known_maintenance_response_counts_as_up(self):
        check = {"url": "u", "expect_header": {"X-MowDeck-Demo": "1"},
                 "maintenance_response": {"status": 503, "header": {"Retry-After": "120"},
                                          "body_contains": "The MowDeck demo is being reset"}}
        reset = response(status=503, headers={"retry-after": "120"},
                         body=b"The MowDeck demo is being reset and will be back in a few minutes.")
        self.assertTrue(model.evaluate_check(check, reset, SETTINGS)["ok"])
        # any other 503, a missing header, a 502 or a timeout stays an outage
        other = [
            response(status=503, headers={"retry-after": "120"}, body=b"Service Unavailable"),
            response(status=503, body=b"The MowDeck demo is being reset"),
            response(status=502, headers={"retry-after": "120"}, body=b"The MowDeck demo is being reset"),
            response(status=None, error="timed out"),
        ]
        for resp in other:
            self.assertFalse(model.evaluate_check(check, resp, SETTINGS)["ok"])

    def test_slow_and_tls_warnings_keep_ok(self):
        slow = model.evaluate_check({"url": "u"}, response(latency=9000), SETTINGS)
        self.assertTrue(slow["ok"] and slow["slow"])
        tls = model.evaluate_check({"url": "u"}, response(tls=3), SETTINGS)
        self.assertTrue(tls["ok"] and tls["tls_warning"])


class ClassifyTest(unittest.TestCase):
    def test_all_ok(self):
        self.assertEqual(model.classify_component([ok()], 0, 2), "operational")

    def test_single_failure_is_only_degraded(self):
        self.assertEqual(model.classify_component([fail()], 1, 2), "degraded")

    def test_confirmed_full_failure_is_major(self):
        self.assertEqual(model.classify_component([fail()], 2, 2), "major")

    def test_confirmed_partial_failure_is_partial(self):
        self.assertEqual(model.classify_component([ok(), fail()], 2, 2), "partial")

    def test_slow_is_degraded(self):
        self.assertEqual(model.classify_component([ok(latency=9000)], 0, 2), "degraded")

    def test_worst(self):
        self.assertEqual(model.worst(["operational", "maintenance", "partial"]), "partial")
        self.assertEqual(model.worst([]), "operational")


class ApplyRunTest(unittest.TestCase):
    def run_sequence(self, sequence, start=T0, step=timedelta(minutes=5)):
        cfg = config()
        data = model.empty_data()
        all_events = []
        for n, web in enumerate(sequence):
            results = {"web": [web], "app": [ok(), ok()]}
            data, events = model.apply_run(data, cfg, results, start + n * step)
            all_events.append(events)
        return data, all_events

    def test_rollup_counts_and_uptime(self):
        data, _ = self.run_sequence([ok(100), ok(300), fail(), ok(200)])
        day = data["history"]["days"]["web"]["2026-10-30"]
        self.assertEqual(day["n"], 4)
        self.assertEqual(day["fail"], 1)
        self.assertEqual(day["lat_n"], 3)
        self.assertEqual(day["lat_sum"], 600)
        self.assertEqual(day["lat_max"], 300)
        self.assertAlmostEqual(model.day_uptime(day), 75.0)

    def test_partial_component_failure_counts_fractionally(self):
        cfg = config()
        data, _ = model.apply_run(model.empty_data(), cfg, {"web": [ok()], "app": [ok(), fail()]}, T0)
        self.assertAlmostEqual(model.day_uptime(data["history"]["days"]["app"]["2026-10-30"]), 50.0)

    def test_planned_component_is_not_recorded(self):
        data, _ = self.run_sequence([ok()])
        self.assertEqual(data["status"]["components"]["soon"]["status"], "planned")
        self.assertNotIn("soon", data["history"]["days"])

    def test_one_blip_opens_no_incident(self):
        data, events = self.run_sequence([ok(), fail(), ok(), ok()])
        self.assertEqual(data["incidents"]["incidents"], [])
        self.assertEqual(events[1]["changed"], ["web"])  # operational -> degraded is still shown

    def test_incident_opens_after_threshold_and_resolves_after_recovery(self):
        data, events = self.run_sequence([ok(), fail(), fail(), fail(), ok(), ok(), ok()])
        incidents = data["incidents"]["incidents"]
        self.assertEqual(len(incidents), 1)
        incident = incidents[0]
        self.assertEqual(events[2]["opened"], [incident["id"]])
        self.assertEqual(events[5]["resolved"], [incident["id"]])  # second success in a row
        self.assertEqual(events[4]["resolved"], [])
        self.assertTrue(incident["auto"])
        self.assertEqual(incident["impact"], "critical")
        self.assertEqual(incident["created_at"], "2026-10-30T12:05:00Z")  # first failed run
        self.assertEqual(incident["resolved_at"], "2026-10-30T12:25:00Z")
        self.assertEqual([u["status"] for u in incident["updates"]], ["investigating", "resolved"])
        self.assertIn("20 min", incident["updates"][-1]["body"])
        self.assertIsNone(data["status"]["components"]["web"]["open_incident"])

    def test_flapping_during_recovery_keeps_incident_open(self):
        data, _ = self.run_sequence([fail(), fail(), ok(), fail(), ok()])
        self.assertIsNone(data["incidents"]["incidents"][0]["resolved_at"])

    def test_a_new_outage_opens_a_new_incident(self):
        data, _ = self.run_sequence([fail(), fail(), ok(), ok(), fail(), fail()])
        self.assertEqual(len(data["incidents"]["incidents"]), 2)

    def test_retention_drops_old_days_and_old_resolved_incidents(self):
        data, _ = self.run_sequence([fail(), fail(), ok(), ok()])
        cfg = config()
        later = T0 + timedelta(days=91)
        data, _ = model.apply_run(data, cfg, {"web": [ok()], "app": [ok(), ok()]}, later)
        self.assertNotIn("2026-10-30", data["history"]["days"]["web"])
        self.assertIn(later.strftime("%Y-%m-%d"), data["history"]["days"]["web"])
        self.assertEqual(data["incidents"]["incidents"], [])

    def test_uptime_reset_clears_a_day_once(self):
        data, _ = self.run_sequence([ok(), fail(), fail(), ok()])
        cfg = dict(config(), uptime_resets=["2026-10-30"])
        later = T0 + timedelta(hours=1)
        data, _ = model.apply_run(data, cfg, {"web": [ok()], "app": [ok(), fail()]}, later)
        web = data["history"]["days"]["web"]["2026-10-30"]
        self.assertEqual((web["n"], web["fail"]), (5, 0))
        self.assertAlmostEqual(model.day_uptime(web), 100.0)
        # the run that applied the reset was recorded after it and counts normally
        self.assertAlmostEqual(model.day_uptime(data["history"]["days"]["app"]["2026-10-30"]), 90.0)
        self.assertEqual(data["history"]["resets_applied"], ["2026-10-30"])
        # later failures on the same day are not wiped again
        data, _ = model.apply_run(data, cfg, {"web": [fail()], "app": [ok(), ok()]}, later + timedelta(minutes=5))
        self.assertEqual(data["history"]["days"]["web"]["2026-10-30"]["fail"], 1)

    def test_removed_component_history_is_dropped(self):
        data, _ = self.run_sequence([ok()])
        data["history"]["days"]["gone"] = {"2026-10-30": {"n": 1}}
        data, _ = model.apply_run(data, config(), {"web": [ok()], "app": [ok(), ok()]}, T0)
        self.assertNotIn("gone", data["history"]["days"])


class PublishTest(unittest.TestCase):
    quiet = {"changed": [], "opened": [], "resolved": []}

    def test_publishes_on_change(self):
        events = dict(self.quiet, changed=["web"])
        self.assertTrue(model.should_publish(events, model.iso(T0), T0, 30))

    def test_throttles_quiet_runs(self):
        self.assertFalse(model.should_publish(self.quiet, model.iso(T0), T0 + timedelta(minutes=10), 30))
        self.assertTrue(model.should_publish(self.quiet, model.iso(T0), T0 + timedelta(minutes=30), 30))

    def test_first_run_publishes(self):
        self.assertTrue(model.should_publish(self.quiet, None, T0, 30))


class ConfigTest(unittest.TestCase):
    def test_rejects_unknown_check_keys(self):
        raw = {"site": {"title": "t", "base_url": "https://x"}, "groups": [
            {"name": "g", "components": [{"id": "a", "name": "A", "checks": [{"url": "https://x", "expect": 1}]}]}]}
        with self.assertRaises(ConfigError):
            validate_config(raw)

    def test_rejects_duplicate_ids_and_plain_http(self):
        comp = {"id": "a", "name": "A", "checks": [{"url": "https://x"}]}
        with self.assertRaises(ConfigError):
            validate_config({"site": {"title": "t", "base_url": "https://x"},
                             "groups": [{"name": "g", "components": [comp, comp]}]})
        with self.assertRaises(ConfigError):
            validate_config({"site": {"title": "t", "base_url": "https://x"},
                             "groups": [{"name": "g", "components": [
                                 {"id": "a", "name": "A", "checks": [{"url": "http://x"}]}]}]})

    def test_maintenance_response_needs_something_specific(self):
        check = {"url": "https://x", "maintenance_response": {"status": 503}}
        with self.assertRaises(ConfigError):
            validate_config({"site": {"title": "t", "base_url": "https://x"}, "groups": [
                {"name": "g", "components": [{"id": "a", "name": "A", "checks": [check]}]}]})

    def test_rejects_malformed_uptime_resets(self):
        with self.assertRaises(ConfigError):
            validate_config({"site": {"title": "t", "base_url": "https://x"}, "uptime_resets": ["31.10.2026"],
                             "groups": [{"name": "g", "components": [
                                 {"id": "a", "name": "A", "checks": [{"url": "https://x"}]}]}]})

    def test_repository_config_is_valid(self):
        from pathlib import Path

        from statuspage.config import load_config

        load_config(Path(__file__).resolve().parent.parent / "config.json")


if __name__ == "__main__":
    unittest.main()
