import socket
import ssl
import unittest

from statuspage import diagnosis as d


class ClassifyPhaseTest(unittest.TestCase):
    def phase(self, failure=d.FAIL_TRANSPORT, stage=None, exc=None, slow=False):
        return d.classify_phase(failure, stage, exc, slow)

    def test_ok_has_no_phase_unless_slow(self):
        self.assertIsNone(self.phase(None))
        self.assertEqual(self.phase(None, slow=True), d.SLOW)

    def test_answered_but_wrong(self):
        self.assertEqual(self.phase(d.FAIL_STATUS), d.HTTP_STATUS)
        self.assertEqual(self.phase(d.FAIL_BODY), d.BODY)

    def test_dns(self):
        self.assertEqual(self.phase(stage=d.STAGE_CONNECT, exc=socket.gaierror(-2, "unknown")), d.DNS)

    def test_connect(self):
        for exc in (ConnectionRefusedError(), TimeoutError(), OSError(113, "no route to host")):
            self.assertEqual(self.phase(stage=d.STAGE_CONNECT, exc=exc), d.CONNECT, exc)

    def test_nothing_attempted_counts_as_connect(self):
        self.assertEqual(self.phase(stage=None, exc=ValueError("bad url")), d.CONNECT)

    def test_tls(self):
        verify = ssl.SSLCertVerificationError(1, "certificate verify failed")
        self.assertEqual(self.phase(stage=d.STAGE_TLS, exc=verify), d.TLS)
        self.assertEqual(self.phase(stage=d.STAGE_TLS, exc=ssl.SSLError("wrong version")), d.TLS)
        # a reset or a timeout while the handshake runs is a handshake problem, not a connect one
        self.assertEqual(self.phase(stage=d.STAGE_TLS, exc=ConnectionResetError()), d.TLS)
        self.assertEqual(self.phase(stage=d.STAGE_TLS, exc=TimeoutError()), d.TLS)

    def test_connected_but_no_headers(self):
        self.assertEqual(self.phase(stage=d.STAGE_RESPONSE, exc=TimeoutError()), d.TIMEOUT_RESPONSE)
        self.assertEqual(self.phase(stage=d.STAGE_RESPONSE, exc=socket.timeout()), d.TIMEOUT_RESPONSE)

    def test_connected_but_broken(self):
        self.assertEqual(self.phase(stage=d.STAGE_RESPONSE, exc=ConnectionResetError()), d.RESPONSE_ERROR)
        self.assertEqual(self.phase(stage=d.STAGE_BODY, exc=TimeoutError()), d.RESPONSE_ERROR)

    def test_every_phase_has_public_wording(self):
        self.assertEqual(set(d.PHASES), set(d.PHASE_TEXT))


class EdgeIdsTest(unittest.TestCase):
    def test_reads_ray_and_cache_status(self):
        found = d.edge_ids({"cf-ray": "9a1b2c3d4e5f-FRA", "cf-cache-status": "HIT", "server": "cloudflare"})
        self.assertEqual(found, {"cf_ray": "9a1b2c3d4e5f-FRA", "cf_cache_status": "HIT"})

    def test_absent_headers(self):
        self.assertEqual(d.edge_ids({}), {"cf_ray": None, "cf_cache_status": None})
        self.assertEqual(d.edge_ids(None), {"cf_ray": None, "cf_cache_status": None})

    def test_odd_values_are_dropped(self):
        found = d.edge_ids({"cf-ray": "abc\r\nSet-Cookie: x", "cf-cache-status": "x" * 200})
        self.assertEqual(found, {"cf_ray": None, "cf_cache_status": None})

    def test_diagnose_keeps_only_the_named_fields(self):
        diag = d.diagnose(
            d.SLOW,
            {"connect_ms": 1, "tls_ms": 2, "ttfb_ms": 3, "total_ms": 4, "peer": "203.0.113.9"},
            {"cf-ray": "A-FRA", "set-cookie": "s=1"},
            True,
        )
        self.assertEqual(
            diag,
            {"phase": "slow", "answered": True, "connect_ms": 1, "tls_ms": 2, "ttfb_ms": 3, "total_ms": 4,
             "cf_ray": "A-FRA", "cf_cache_status": None},
        )
