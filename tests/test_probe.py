import socket
import ssl
import threading
import unittest
from unittest import mock

from statuspage import diagnosis as d
from statuspage import model, probe

from .tlsserver import TestServer, requires_openssl, respond, stall

SETTINGS = {"slow_ms": 5000, "tls_warn_days": 7}


def judged(check, response):
    return model.evaluate_check(check, response, SETTINGS)


def refused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class TlsContextTest(unittest.TestCase):
    def test_context_verifies_and_requires_tls_12(self):
        context = probe.tls_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)


class NoServerTest(unittest.TestCase):
    def test_connection_refused_is_connect(self):
        # Windows retries a refused connection for a couple of seconds, hence the generous timeout.
        response = probe.fetch(f"https://127.0.0.1:{refused_port()}/", 10)
        self.assertEqual(judged({"url": "u"}, response)["diag"]["phase"], d.CONNECT)
        self.assertFalse(response["answered"])
        self.assertIsNone(response["timings"]["connect_ms"])

    def test_unresolvable_name_is_dns(self):
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror(-2, "Name or service not known")):
            response = probe.fetch("https://name-that-does-not-exist.invalid/", 5)
        self.assertEqual(response["error"], "DNS lookup failed")
        self.assertEqual(judged({"url": "u"}, response)["diag"]["phase"], d.DNS)

    def test_server_that_does_not_speak_tls_is_tls(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)

            def plain_server():
                conn, _ = listener.accept()
                conn.recv(1024)
                conn.sendall(b"HTTP/1.0 200 OK\r\n\r\nplain")
                conn.close()

            thread = threading.Thread(target=plain_server, daemon=True)
            thread.start()
            response = probe.fetch(f"https://127.0.0.1:{listener.getsockname()[1]}/", 3)
            thread.join(3)
        self.assertEqual(response["error"], "TLS handshake failed")
        self.assertEqual(judged({"url": "u"}, response)["diag"]["phase"], d.TLS)
        self.assertIsNotNone(response["timings"]["connect_ms"])


@requires_openssl
class LocalServerTest(unittest.TestCase):
    def fetch(self, server, timeout=3):
        return probe.fetch(server.url, timeout, server.trusting_context())

    def test_ok_records_timings_and_cloudflare_ids(self):
        headers = {"CF-RAY": "9a1b-FRA", "CF-Cache-Status": "DYNAMIC"}
        with TestServer(lambda r: respond(r, headers=headers)) as server:
            response = self.fetch(server)
        result = judged({"url": "u", "expect_body_contains": "hello"}, response)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["diag"]["phase"])
        self.assertEqual((result["diag"]["cf_ray"], result["diag"]["cf_cache_status"]), ("9a1b-FRA", "DYNAMIC"))
        timings = response["timings"]
        self.assertTrue(all(timings[k] is not None for k in ("connect_ms", "tls_ms", "ttfb_ms", "total_ms")))

    def test_headers_that_never_come_are_timeout_response_without_ray(self):
        with TestServer(stall) as server:
            response = self.fetch(server, timeout=0.4)
        result = judged({"url": "u"}, response)
        self.assertEqual(result["error"], "timed out")
        self.assertEqual(result["diag"]["phase"], d.TIMEOUT_RESPONSE)
        self.assertFalse(result["diag"]["answered"])
        self.assertIsNone(result["diag"]["cf_ray"])
        self.assertIsNotNone(result["diag"]["connect_ms"])
        self.assertIsNotNone(result["diag"]["tls_ms"])
        self.assertIsNone(result["diag"]["ttfb_ms"])

    def test_wrong_status_is_http_status_and_keeps_the_ray(self):
        with TestServer(lambda r: respond(r, 502, b"bad gateway", {"CF-RAY": "502-FRA"})) as server:
            response = self.fetch(server)
        result = judged({"url": "u"}, response)
        self.assertEqual((result["error"], result["diag"]["phase"]), ("HTTP 502", d.HTTP_STATUS))
        self.assertEqual(result["diag"]["cf_ray"], "502-FRA")
        self.assertTrue(result["diag"]["answered"])

    def test_missing_text_and_header_are_body(self):
        with TestServer(lambda r: respond(r, body=b"something else")) as server:
            response = self.fetch(server)
        text = judged({"url": "u", "expect_body_contains": "PiWatch"}, response)
        header = judged({"url": "u", "expect_header": {"X-Demo": "1"}}, response)
        self.assertEqual((text["diag"]["phase"], header["diag"]["phase"]), (d.BODY, d.BODY))

    def test_slow_answer_is_slow(self):
        with TestServer(lambda r: respond(r)) as server:
            response = self.fetch(server)
        response["latency_ms"] = 6200
        result = judged({"url": "u"}, response)
        self.assertTrue(result["ok"] and result["slow"])
        self.assertEqual(result["diag"]["phase"], d.SLOW)

    def test_untrusted_certificate_is_still_refused_and_tls(self):
        with TestServer(lambda r: respond(r)) as server:
            response = probe.fetch(server.url, 3)  # default context: the self-signed cert is not trusted
        self.assertIsNone(response["status"])
        self.assertTrue(response["error"].startswith("TLS certificate invalid"), response["error"])
        self.assertEqual(judged({"url": "u"}, response)["diag"]["phase"], d.TLS)
        self.assertEqual(server.requests, 0)

    def test_connection_dropped_after_the_request_is_response_error(self):
        with TestServer(lambda request: request.connection.close()) as server:
            response = self.fetch(server)
        self.assertEqual(judged({"url": "u"}, response)["diag"]["phase"], d.RESPONSE_ERROR)

    def test_redirects_are_followed(self):
        def behaviour(request):
            if request.path == "/":
                respond(request, 302, b"", {"Location": "/final"})
            else:
                respond(request, headers={"CF-RAY": "final-FRA"})

        with TestServer(behaviour) as server:
            response = self.fetch(server)
        self.assertEqual(response["status"], 200)
        self.assertEqual(response["headers"]["cf-ray"], "final-FRA")

    def test_retry_reports_both_attempts(self):
        def behaviour(request):
            if request.server.requests == 1:
                stall(request)
            else:
                respond(request, 500, b"oops", {"CF-RAY": "second-FRA"})

        with TestServer(behaviour) as server:
            context = server.trusting_context()
            responses = probe.probe_all(
                [{"url": server.url}], 0.4, 0, fetcher=lambda url, t: probe.fetch(url, t, context),
                tls_probe=lambda host, t: None,
            )
        result = judged({"url": server.url}, responses[0])
        self.assertEqual([a["phase"] for a in result["attempts"]], [d.TIMEOUT_RESPONSE, d.HTTP_STATUS])
        self.assertEqual([a["cf_ray"] for a in result["attempts"]], [None, "second-FRA"])
        self.assertEqual(result["diag"], result["attempts"][-1])


class RetryTest(unittest.TestCase):
    """probe_all with a fake fetcher: the retry rules stay as they were."""

    def run_probe(self, answers):
        calls = []

        def fetcher(url, timeout):
            calls.append(url)
            return dict(answers[len(calls) - 1])

        out = probe.probe_all([{"url": "https://a.example.org"}], 1, 0, fetcher=fetcher, tls_probe=lambda h, t: 9)
        return out[0], len(calls)

    def good(self, status=200):
        return {"status": status, "headers": {}, "body": b"", "latency_ms": 5, "error": None}

    def bad(self):
        return {"status": None, "headers": {}, "body": b"", "latency_ms": None, "error": "timed out"}

    def test_success_is_not_retried(self):
        response, calls = self.run_probe([self.good()])
        self.assertEqual(calls, 1)
        self.assertNotIn("previous", response)
        self.assertEqual(response["tls_days_left"], 9)

    def test_error_and_5xx_are_retried_once_and_keep_the_first_attempt(self):
        response, calls = self.run_probe([self.bad(), self.bad(), self.good()])
        self.assertEqual(calls, 2)
        self.assertEqual(response["previous"]["error"], "timed out")
        response, calls = self.run_probe([self.good(503), self.good(200)])
        self.assertEqual((calls, response["status"], response["previous"]["status"]), (2, 200, 503))

    def test_4xx_is_not_retried(self):
        self.assertEqual(self.run_probe([self.good(404)])[1], 1)


if __name__ == "__main__":
    unittest.main()
