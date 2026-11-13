import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from statuspage import __main__ as cli
from statuspage import model, report, schedule
from statuspage.config import ConfigError, validate_config

from .test_model import SETTINGS, T0, ok

PROBE = {
    "name": "Answer probe",
    "url": "https://lktec.example.org/api/chat/probe",
    "method": "POST",
    "headers_from_env": {"X-Status-Probe": "CHAT_PROBE_TOKEN"},
    "interval_minutes": 1440,
    "timeout_seconds": 45,
    "slow_ms": 20000,
    "expect_json": {"ok": True},
    "degraded_json": {"degraded": True},
    "degraded_note": "answered by the fallback model",
    "report_json_fields": ["provider", "degraded", "reason"],
}
HEALTH = {"name": "Health", "url": "https://lktec.example.org/api/chat/health", "expect_json": {"ok": True}}
ENV = {"CHAT_PROBE_TOKEN": "s3cret-token-value"}


def chat_config():
    return validate_config(
        {
            "site": {"title": "t", "base_url": "https://status.example.org"},
            "settings": SETTINGS,
            "groups": [{"name": "Website", "components": [{"id": "chat", "name": "Chat", "checks": [HEALTH, PROBE]}]}],
        }
    )


def response(status=200, body=b'{"ok": true}', latency=3000):
    return {"status": status, "headers": {}, "body": body, "latency_ms": latency, "error": None, "tls_days_left": 60}


def run(data, results, now):
    return model.apply_run(data, chat_config(), {"chat": results}, now)


class PlanTest(unittest.TestCase):
    def test_check_without_interval_is_always_probed(self):
        self.assertEqual(schedule.plan_check(HEALTH, None, T0, {}), (schedule.PROBE, None))

    def test_first_run_probes_at_once(self):
        # no previous result: the probe runs in the first run after the check was added
        self.assertEqual(schedule.plan_check(PROBE, None, T0, ENV)[0], schedule.PROBE)
        state = {"checks": [{"name": "Health", "ok": True}]}
        self.assertEqual(schedule.plan_check(PROBE, state, T0, ENV)[0], schedule.PROBE)
        # a stored entry without a timestamp (older data) is not a previous result
        state = {"checks": [{"name": "Answer probe", "ok": True}]}
        self.assertEqual(schedule.plan_check(PROBE, state, T0, ENV)[0], schedule.PROBE)

    def test_not_due_is_carried_and_due_is_probed(self):
        stored = {"name": "Answer probe", "ok": True, "checked_at": model.iso(T0)}
        state = {"checks": [stored]}
        self.assertEqual(schedule.plan_check(PROBE, state, T0 + timedelta(hours=23, minutes=59), ENV),
                         (schedule.CARRY, stored))
        self.assertEqual(schedule.plan_check(PROBE, state, T0 + timedelta(days=1), ENV)[0], schedule.PROBE)

    def test_missing_secret_skips_only_that_check(self):
        self.assertEqual(schedule.plan_check(PROBE, None, T0, {}), (schedule.SKIP, None))
        self.assertEqual(schedule.plan_check(PROBE, None, T0, {"CHAT_PROBE_TOKEN": ""})[0], schedule.SKIP)
        self.assertEqual(schedule.missing_env(PROBE, {}), ["CHAT_PROBE_TOKEN"])
        self.assertEqual(schedule.plan_check(HEALTH, None, T0, {})[0], schedule.PROBE)

    def test_headers_resolve_from_the_environment(self):
        self.assertEqual(schedule.resolve_headers(PROBE, ENV), {"X-Status-Probe": "s3cret-token-value"})
        self.assertIsNone(schedule.resolve_headers(HEALTH, ENV))


class CarryTest(unittest.TestCase):
    def test_probe_result_is_stored_with_its_time_and_carried_forward(self):
        probe = model.evaluate_check(PROBE, response(), SETTINGS)
        data, _ = run(model.empty_data(), [ok(), probe], T0)
        stored = data["status"]["components"]["chat"]["checks"][1]
        self.assertEqual(stored["checked_at"], model.iso(T0))
        self.assertNotIn("checked_at", data["status"]["components"]["chat"]["checks"][0])

        later = T0 + timedelta(minutes=5)
        action, previous = schedule.plan_check(PROBE, data["status"]["components"]["chat"], later, ENV)
        self.assertEqual(action, schedule.CARRY)
        carried = schedule.carried_result(previous)
        data, _ = run(data, [ok(), carried], later)
        state = data["status"]["components"]["chat"]
        self.assertEqual(state["status"], "operational")
        # the carried result keeps the time it was really probed, so the interval does not slide
        self.assertEqual(state["checks"][1]["checked_at"], model.iso(T0))

    def test_degraded_probe_shows_degraded_and_is_carried_as_such(self):
        probe = model.evaluate_check(PROBE, response(body=b'{"ok": true, "provider": "openai", "degraded": true}'), SETTINGS)
        self.assertTrue(probe["ok"] and probe["degraded"])
        data, _ = run(model.empty_data(), [ok(), probe], T0)
        state = data["status"]["components"]["chat"]
        self.assertEqual(state["status"], "degraded")
        self.assertIn("answered by the fallback model", state["message"])
        carried = schedule.carried_result(state["checks"][1])
        data, _ = run(data, [ok(), carried], T0 + timedelta(minutes=5))
        self.assertEqual(data["status"]["components"]["chat"]["status"], "degraded")
        day = data["history"]["days"]["chat"][T0.strftime("%Y-%m-%d")]
        self.assertAlmostEqual(model.day_uptime(day), 100.0)  # degraded is up

    def test_failed_probe_counts_by_the_failure_threshold(self):
        probe = model.evaluate_check(PROBE, response(status=503, body=b'{"ok": false, "reason": "upstream_error"}'), SETTINGS)
        self.assertFalse(probe["ok"])
        data, events = run(model.empty_data(), [ok(), probe], T0)
        self.assertEqual(data["status"]["components"]["chat"]["status"], "degraded")  # one failed run
        carried = schedule.carried_result(data["status"]["components"]["chat"]["checks"][1])
        data, events = run(data, [ok(), carried], T0 + timedelta(minutes=5))
        self.assertEqual(data["status"]["components"]["chat"]["status"], "partial")
        self.assertEqual(len(events["opened"]), 1)

    def test_failed_health_is_down(self):
        health = model.evaluate_check(HEALTH, response(status=503, body=b'{"ok": false}'), SETTINGS)
        probe = model.evaluate_check(PROBE, response(status=503, body=b'{"ok": false}'), SETTINGS)
        data, _ = run(model.empty_data(), [health, probe], T0)
        data, _ = run(data, [health, schedule.carried_result(data["status"]["components"]["chat"]["checks"][1])],
                      T0 + timedelta(minutes=5))
        self.assertEqual(data["status"]["components"]["chat"]["status"], "major")

    def test_own_slow_threshold(self):
        self.assertFalse(model.evaluate_check(PROBE, response(latency=9000), SETTINGS)["slow"])
        self.assertTrue(model.evaluate_check(PROBE, response(latency=21000), SETTINGS)["slow"])


class LogTest(unittest.TestCase):
    def test_fresh_probe_line_has_outcome_status_latency_and_fields_but_no_secret(self):
        body = b'{"ok": true, "provider": "anthropic", "ttftMs": 800, "latencyMs": 2900, "secret": "x"}'
        result = model.evaluate_check(PROBE, response(body=body), SETTINGS)
        line = report.scheduled_line("portfolio-chat", result)
        self.assertIn("Answer probe: probed now, ok (HTTP 200; 3000 ms; provider=anthropic)", line)
        self.assertNotIn("secret", line)
        self.assertNotIn(ENV["CHAT_PROBE_TOKEN"], line)

    def test_failed_probe_line_names_the_reason(self):
        body = b'{"ok": false, "reason": "unexpected_answer"}'
        result = model.evaluate_check(PROBE, response(status=503, body=body), SETTINGS)
        self.assertIn("probed now, failed: HTTP 503 (HTTP 503; 3000 ms; reason=unexpected_answer)",
                      report.scheduled_line("portfolio-chat", result))

    def test_carried_line_names_the_probe_time_and_the_next(self):
        carried = schedule.carried_result({"name": "Answer probe", "ok": True, "degraded": True,
                                           "degraded_note": "answered by the fallback model",
                                           "checked_at": "2026-10-30T12:00:00Z"})
        line = report.scheduled_line("portfolio-chat", carried, "2026-10-31T12:00:00Z")
        self.assertIn("Answer probe: degraded: answered by the fallback model "
                      "(probed 2026-10-30T12:00:00Z, next probe at 2026-10-31T12:00:00Z)", line)
        state = {"status": "degraded", "latency_ms": 100, "message": ""}
        self.assertIn("result of 2026-10-30T12:00:00Z", report.component_line("portfolio-chat", state, [carried]))

    def test_skipped_line_is_a_warning_with_the_variable_name_only(self):
        line = report.skipped_line("portfolio-chat", PROBE, ["CHAT_PROBE_TOKEN"])
        self.assertTrue(line.startswith("::warning::portfolio-chat: check 'Answer probe' skipped"))
        self.assertIn("CHAT_PROBE_TOKEN not set", line)

    def test_report_fields_only_from_a_json_object_and_cut(self):
        self.assertEqual(model.report_fields(response(body=b"<html>"), ["reason"]), {})
        self.assertEqual(model.report_fields(response(body=b"[1]"), ["reason"]), {})
        long = model.report_fields(response(body=('{"reason": "' + "x" * 500 + '"}').encode()), ["reason"])
        self.assertEqual(len(long["reason"]), model.REPORT_VALUE_MAX)
        nested = model.report_fields(response(body=b'{"reason": {"a": 1}}'), ["reason", "missing"])
        self.assertEqual(nested, {})


class CheckCommandTest(unittest.TestCase):
    """`python -m statuspage check` end to end with a fake network: the first run probes at once
    and logs the outcome, the next run carries it, a missing secret only skips the probe."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        raw = {"site": {"title": "t", "base_url": "https://status.example.org"}, "settings": SETTINGS,
               "groups": [{"name": "Website", "components": [{"id": "chat", "name": "Chat", "checks": [HEALTH, PROBE]}]}]}
        self.config = root / "config.json"
        self.config.write_text(json.dumps(raw), encoding="utf-8")
        self.data = root / "data"
        self.requests = []

    def fake_probe_all(self, checks, timeout, retry_delay, headers=None):
        self.requests.append([(c["name"], h) for c, h in zip(checks, headers)])
        body = {"Health": b'{"ok": true}', "Answer probe": b'{"ok": true, "provider": "anthropic", "ttftMs": 700}'}
        return [response(body=body[c["name"]]) for c in checks]

    def check(self, environ):
        out = io.StringIO()
        with mock.patch.object(cli, "probe_all", self.fake_probe_all),                 mock.patch.dict(os.environ, environ, clear=False), contextlib.redirect_stdout(out):
            if "CHAT_PROBE_TOKEN" not in environ:
                os.environ.pop("CHAT_PROBE_TOKEN", None)
            cli.main(["--config", str(self.config), "--data", str(self.data), "check"])
        return out.getvalue()

    def test_first_run_probes_and_logs_then_carries(self):
        first = self.check(ENV)
        self.assertEqual(self.requests[0], [("Health", None), ("Answer probe", {"X-Status-Probe": ENV["CHAT_PROBE_TOKEN"]})])
        self.assertIn("Answer probe: probed now, ok (HTTP 200; 3000 ms; provider=anthropic)", first)
        self.assertNotIn(ENV["CHAT_PROBE_TOKEN"], first)
        self.assertNotIn(ENV["CHAT_PROBE_TOKEN"], (self.data / "status.json").read_text(encoding="utf-8"))

        second = self.check(ENV)
        self.assertEqual(self.requests[1], [("Health", None)])  # not fetched again the same day
        self.assertIn("Answer probe: ok (probed ", second)
        self.assertIn("next probe at", second)

    def test_missing_secret_skips_the_probe_with_a_warning(self):
        out = self.check({})
        self.assertEqual(self.requests[0], [("Health", None)])
        self.assertIn("::warning::chat: check 'Answer probe' skipped, CHAT_PROBE_TOKEN not set", out)
        status = json.loads((self.data / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["components"]["chat"]["status"], "operational")


class ConfigOptionsTest(unittest.TestCase):
    def validate(self, check):
        return validate_config({"site": {"title": "t", "base_url": "https://x"},
                                "groups": [{"name": "g", "components": [{"id": "a", "name": "A", "checks": [check]}]}]})

    def test_valid_probe_check(self):
        self.validate(dict(PROBE))

    def test_rejects_bad_options(self):
        bad = [
            {"method": "DELETE"},
            {"headers_from_env": {"X-Status-Probe": "lowercase"}},
            {"headers_from_env": {}},
            {"interval_minutes": 1},
            {"timeout_seconds": 0},
            {"slow_ms": "fast"},
            {"degraded_json": {}},
            {"degraded_note": ""},
            {"report_json_fields": "provider"},
        ]
        for change in bad:
            with self.assertRaises(ConfigError, msg=change):
                self.validate({**PROBE, **change})

    def test_interval_check_needs_a_unique_name(self):
        unnamed = {k: v for k, v in PROBE.items() if k != "name"}
        with self.assertRaises(ConfigError):
            self.validate(unnamed)
        with self.assertRaises(ConfigError):
            validate_config({"site": {"title": "t", "base_url": "https://x"}, "groups": [{"name": "g", "components": [
                {"id": "a", "name": "A", "checks": [PROBE, {**HEALTH, "name": "Answer probe"}]}]}]})


if __name__ == "__main__":
    unittest.main()
