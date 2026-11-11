"""A throwaway HTTPS server on loopback for the probe tests: no real network involved.

The certificate is self-signed and made with the `openssl` command line tool; tests that need
it skip when the tool is missing (CI has it). A behaviour is a function `(request) -> None`
that answers through `respond` or stalls; `server.release` ends a stall at teardown.
"""

from __future__ import annotations

import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

OPENSSL = shutil.which("openssl")


def make_certificate(directory: Path) -> tuple[Path, Path]:
    cert, key = directory / "cert.pem", directory / "key.pem"
    subprocess.run(
        [OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=localhost",
         "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost", "-keyout", str(key), "-out", str(cert)],
        check=True, capture_output=True,
    )
    return cert, key


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass  # clients that reject the certificate or hang up are expected here


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_GET(self):  # noqa: N802
        self.server.requests += 1
        self.server.behaviour(self)

    def log_message(self, *args):
        pass


class TestServer:
    def __init__(self, behaviour):
        self.directory = tempfile.TemporaryDirectory()
        self.cert, key = make_certificate(Path(self.directory.name))
        self.httpd = QuietServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert, key)
        # The handshake happens in the handler thread on the first read, not in accept().
        self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True, do_handshake_on_connect=False)
        self.httpd.behaviour = behaviour
        self.httpd.requests = 0
        self.httpd.release = threading.Event()
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.httpd.server_address[1]}/"

    @property
    def requests(self) -> int:
        return self.httpd.requests

    def trusting_context(self) -> ssl.SSLContext:
        context = ssl.create_default_context(cafile=str(self.cert))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.directory.cleanup()


def respond(request, status=200, body=b"hello", headers=None):
    request.send_response(status)
    for name, value in (headers or {}).items():
        request.send_header(name, value)
    request.send_header("Content-Length", str(len(body)))
    request.end_headers()
    request.wfile.write(body)


def stall(request):
    request.server.release.wait(10)


requires_openssl = unittest.skipUnless(OPENSSL, "the openssl command line tool is needed for the test certificate")
