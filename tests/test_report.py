import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from statuspage import diagnosis as d
from statuspage import model, render, report

from .test_model import SETTINGS, T0, config, ok

ROOT = Path(__file__).resolve().parent.parent
RAY = "9a1b2c3d4e5f6a7b-FRA"
OTHER_RAY = "1122334455667788-AMS"


def diag(phase=None, answered=True, ray=None, cache=None, **timings):
    values = {"connect_ms": None, "tls_ms": None, "ttfb_ms": None, "total_ms": None, **timings}
    return {"phase": phase, "answered": answered, "cf_ray": ray, "cf_cache_status": cache, **values}


def failed(error, *attempts, name="Web app"):
    return {"name": name, "ok": False, "slow": False, "tls_warning": False, "latency_ms": None,
            "http_status": None, "tls_days_left": 60, "error": error, "diag": attempts[-1],
            "attempts": list(attempts)}


def healthy(attempt, name="Web app", latency=409):
    result = ok(latency)
    return {**result, "name": name, "diag": attempt, "attempts": [attempt]}


def state(status, latency=None, message=""):
    return {"status": status, "latency_ms": latency, "message": message}


class LogLineTest(unittest.TestCase):
    def line(self, status, checks, latency=None):
        return report.component_line("piwatch-demo", state(status, latency, model.component_message(checks)), checks)

    def test_timeout_without_response(self):
        check = failed("timed out", diag(d.TIMEOUT_RESPONSE, False, connect_ms=43, tls_ms=61, total_ms=15062))
        self.assertEqual(
            self.line("degraded", [check]),
            "piwatch-demo         degraded     Web app: timed out (phase: timeout_response, connect 43 ms, "
            "tls 61 ms, total 15062 ms; no response, so no cf-ray)",
        )

    def test_one_line_example_per_phase(self):
        cases = {
            d.DNS: (failed("DNS lookup failed", diag(d.DNS, False, total_ms=20)),
                    "Web app: DNS lookup failed (phase: dns, total 20 ms; no response, so no cf-ray)"),
            d.CONNECT: (failed("connection refused", diag(d.CONNECT, False, total_ms=3)),
                        "Web app: connection refused (phase: connect, total 3 ms; no response, so no cf-ray)"),
            d.TLS: (failed("TLS handshake failed", diag(d.TLS, False, connect_ms=40, total_ms=90)),
                    "Web app: TLS handshake failed (phase: tls, connect 40 ms, total 90 ms; "
                    "no response, so no cf-ray)"),
            d.HTTP_STATUS: (failed("HTTP 502", diag(d.HTTP_STATUS, True, RAY, "DYNAMIC", ttfb_ms=30)),
                            f"Web app: HTTP 502 (phase: http_status, first byte 30 ms; cf-ray {RAY}, "
                            "cf-cache-status DYNAMIC)"),
            d.BODY: (failed("expected content missing from the response", diag(d.BODY, True, RAY)),
                     f"Web app: expected content missing from the response (phase: body; cf-ray {RAY})"),
        }
        for phase, (check, expected) in cases.items():
            self.assertTrue(self.line("degraded", [check]).endswith(expected), phase)

    def test_slow_check_names_its_phase_and_ray(self):
        check = healthy(diag(d.SLOW, True, RAY, total_ms=6200))
        check.update(slow=True, latency_ms=6200)
        self.assertEqual(
            self.line("degraded", [check], 6200).split(None, 2)[2],
            f"Web app: slow response (6200 ms) (phase: slow, total 6200 ms; cf-ray {RAY})",
        )

    def test_successful_check_shows_the_ray_in_the_log_only(self):
        line = self.line("operational", [healthy(diag(None, True, RAY, "HIT"))], 409)
        self.assertEqual(line.split(None, 2)[2], f"409 ms (cf-ray {RAY}, cf-cache-status HIT)")

    def test_successful_check_without_ray(self):
        line = self.line("operational", [healthy(diag(None, True))], 409)
        self.assertEqual(line.split(None, 2)[2], "409 ms (response without a cf-ray header)")

    def test_both_attempts_are_shown_when_they_differ(self):
        check = failed(
            "HTTP 502",
            diag(d.TIMEOUT_RESPONSE, False, connect_ms=40, tls_ms=60, total_ms=15000),
            diag(d.HTTP_STATUS, True, OTHER_RAY, ttfb_ms=12),
        )
        text = self.line("degraded", [check]).split(None, 2)[2]
        self.assertEqual(
            text,
            "Web app: HTTP 502 (attempt 1: phase: timeout_response, connect 40 ms, tls 60 ms, total 15000 ms; "
            f"no response, so no cf-ray; attempt 2: phase: http_status, first byte 12 ms; cf-ray {OTHER_RAY})",
        )

    def test_several_checks_are_named(self):
        checks = [healthy(diag(None, True, RAY), "Web app"), healthy(diag(None, True, OTHER_RAY), "API")]
        text = self.line("operational", checks, 100).split(None, 2)[2]
        self.assertEqual(text, f"100 ms (Web app: cf-ray {RAY}; API: cf-ray {OTHER_RAY})")

    def test_results_without_diagnostics_and_planned_components(self):
        legacy = {**ok(), "name": "Web app"}
        self.assertEqual(report.component_line("web", state("operational", 120), [legacy]).split(None, 2)[2], "120 ms")
        self.assertEqual(report.component_line("soon", {"status": "planned"}, None).strip(), "soon                 planned")


class PublishedDataTest(unittest.TestCase):
    """What reaches data/ and the site: the phase and its plain wording, nothing else."""

    def run_failing(self, runs=2):
        cfg = config()
        data = model.empty_data()
        events = []
        for n in range(runs):
            check = failed(
                "timed out",
                diag(d.TIMEOUT_RESPONSE, False, connect_ms=43, tls_ms=61, total_ms=15062),
                diag(d.HTTP_STATUS, True, RAY, "DYNAMIC", ttfb_ms=12, connect_ms=44),
            )
            data, ev = model.apply_run(data, cfg, {"web": [check], "app": [ok(), ok()]}, T0 + timedelta(minutes=5 * n))
            events.append(ev)
        return cfg, data

    def test_phase_is_recorded_per_check(self):
        _, data = self.run_failing(1)
        self.assertEqual(data["status"]["components"]["web"]["checks"][0]["phase"], d.HTTP_STATUS)
        self.assertIsNone(data["status"]["components"]["app"]["checks"][0]["phase"])

    def test_incident_text_says_where_it_failed(self):
        _, data = self.run_failing()
        body = data["incidents"]["incidents"][0]["updates"][0]["body"]
        self.assertEqual(
            body,
            "Automated checks for Web are failing. Web app: timed out. Where it failed - "
            "Web app: the server answered with an unexpected status.",
        )

    def test_no_ray_timing_or_address_in_data_or_site(self):
        cfg, data = self.run_failing()
        out = Path(tempfile.mkdtemp()) / "_site"
        render.build_site(cfg, data, [], T0 + timedelta(hours=1), ROOT / "site", out)
        texts = [json.dumps(data)] + [p.read_text(encoding="utf-8") for p in out.rglob("*") if p.suffix in
                                      (".html", ".json", ".xml")]
        for text in texts:
            for forbidden in (RAY, OTHER_RAY, "cf-ray", "cf_ray", "cf-cache", "cf_cache", "DYNAMIC", "15062",
                              "connect_ms", "ttfb_ms"):
                self.assertNotIn(forbidden, text, forbidden)

    def test_old_data_files_still_load_and_roll_forward(self):
        """Files written before the phase existed have no `phase` on their checks."""
        cfg = config()
        old_check = {"name": "Web app", "ok": True, "latency_ms": 100, "http_status": 200,
                     "tls_days_left": 60, "error": None}
        old = model.empty_data()
        old["status"]["components"]["web"] = {
            "status": "operational", "checked_at": "2026-10-30T11:55:00Z", "latency_ms": 100, "message": "",
            "fail_streak": 0, "ok_streak": 3, "first_fail_at": None, "open_incident": None, "checks": [old_check],
        }
        old["history"]["days"]["web"] = {"2026-10-30": {"n": 10, "up": 10.0, "fail": 0, "lat_sum": 900,
                                                       "lat_n": 9, "lat_max": 200}}
        data, _ = model.apply_run(old, cfg, {"web": [ok()], "app": [ok(), ok()]}, T0)
        day = data["history"]["days"]["web"]["2026-10-30"]
        self.assertEqual((day["n"], day["up"]), (11, 11.0))  # the old day carries on counting
        self.assertEqual(set(day), {"n", "up", "fail", "lat_sum", "lat_n", "lat_max"})
        out = Path(tempfile.mkdtemp()) / "_site"
        render.build_site(cfg, old, [], T0, ROOT / "site", out)
        self.assertTrue((out / "index.html").is_file())

    def test_history_format_is_unchanged(self):
        _, data = self.run_failing(1)
        day = data["history"]["days"]["web"][T0.strftime("%Y-%m-%d")]
        self.assertEqual(set(day), {"n", "up", "fail", "lat_sum", "lat_n", "lat_max"})


class RetryDiagnosticsTest(unittest.TestCase):
    def test_evaluate_check_collects_both_attempts(self):
        first = {"status": None, "headers": {}, "body": b"", "latency_ms": None, "error": "timed out",
                 "stage": d.STAGE_RESPONSE, "exception": TimeoutError(), "answered": False,
                 "timings": {"connect_ms": 40, "tls_ms": 60, "ttfb_ms": None, "total_ms": 15000}}
        second = {"status": 502, "headers": {"cf-ray": RAY}, "body": b"", "latency_ms": 20, "error": None,
                  "stage": None, "exception": None, "answered": True,
                  "timings": {"connect_ms": 40, "tls_ms": 60, "ttfb_ms": 20, "total_ms": 125}, "previous": first}
        result = model.evaluate_check({"url": "u"}, second, SETTINGS)
        self.assertEqual([a["phase"] for a in result["attempts"]], [d.TIMEOUT_RESPONSE, d.HTTP_STATUS])
        self.assertEqual(result["diag"]["cf_ray"], RAY)
        self.assertIsNone(result["attempts"][0]["cf_ray"])


if __name__ == "__main__":
    unittest.main()
