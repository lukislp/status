"""The only module that talks to the network: one GET per check, plus one TLS handshake per
host to read the certificate expiry. Standard library only."""

from __future__ import annotations

import functools
import http.client
import socket
import ssl
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlsplit

from . import diagnosis

USER_AGENT = "lktec-status/1.0 (+https://status.lktec.org)"
MAX_BODY = 2 * 1024 * 1024


def tls_context() -> ssl.SSLContext:
    """Certificate-verifying context that refuses anything older than TLS 1.2."""
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


class Trace:
    """What one request learned about itself: how far it got and how long each step took.
    Written by `TracingHTTPSConnection`, read by `fetch`; it makes no decisions."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.started = clock()
        self.stage: str | None = None
        self.connect_ms: int | None = None
        self.tls_ms: int | None = None
        self.ttfb_ms: int | None = None
        self.answered = False
        self.headers: dict = {}
        self._mark = self.started

    def _lap(self) -> int:
        now = self.clock()
        elapsed, self._mark = int((now - self._mark) * 1000), now
        return elapsed

    def begin_connection(self) -> None:
        """A new connection (the first one, or a redirect hop): forget the previous hop."""
        self.stage = diagnosis.STAGE_CONNECT
        self.connect_ms = self.tls_ms = self.ttfb_ms = None
        self.answered = False
        self.headers = {}
        self._mark = self.clock()

    def connected(self) -> None:
        self.connect_ms = self._lap()
        self.stage = diagnosis.STAGE_TLS

    def secured(self) -> None:
        self.tls_ms = self._lap()
        self.stage = diagnosis.STAGE_RESPONSE

    def request_sent(self) -> None:
        self._mark = self.clock()

    def response_headers(self, headers: dict) -> None:
        self.ttfb_ms = self._lap()
        self.answered, self.headers = True, headers
        self.stage = diagnosis.STAGE_BODY

    def timings(self) -> dict:
        return {
            "connect_ms": self.connect_ms,
            "tls_ms": self.tls_ms,
            "ttfb_ms": self.ttfb_ms,
            "total_ms": int((self.clock() - self.started) * 1000),
        }


class TracingHTTPSConnection(http.client.HTTPSConnection):
    """The standard HTTPS connection, with TCP connect, TLS handshake and the wait for the
    response headers kept apart so that a failure can be attributed to one of them."""

    def __init__(self, *args, trace: Trace, **kwargs):
        super().__init__(*args, **kwargs)
        self.trace = trace
        self.tls_context = kwargs["context"]

    def connect(self) -> None:
        self.trace.begin_connection()
        http.client.HTTPConnection.connect(self)  # TCP (and the CONNECT tunnel of a proxy)
        self.trace.connected()
        server_hostname = getattr(self, "_tunnel_host", None) or self.host
        self.sock = self.tls_context.wrap_socket(self.sock, server_hostname=server_hostname)
        self.trace.secured()

    def request(self, *args, **kwargs):
        super().request(*args, **kwargs)
        self.trace.request_sent()

    def getresponse(self):
        response = super().getresponse()
        self.trace.response_headers(lower_headers(response.headers))
        return response


class TracingHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, context: ssl.SSLContext, trace: Trace):
        super().__init__(context=context)
        self.context, self.trace = context, trace

    def https_open(self, req):
        connection = functools.partial(TracingHTTPSConnection, trace=self.trace)
        return self.do_open(connection, req, context=self.context)


def lower_headers(headers) -> dict:
    return {k.lower(): v for k, v in (headers or {}).items()}


def fetch(
    url: str,
    timeout: float,
    context: ssl.SSLContext | None = None,
    method: str = "GET",
    headers: dict | None = None,
) -> dict:
    """One request, a GET unless the check says otherwise (a POST is sent with an empty body).
    Besides the response it returns how far the request got (`stage`), the exception that
    stopped it (`exception`), whether response headers arrived (`answered`) and cheap timings;
    deciding what that means is diagnosis.py's job. `headers` may carry a secret (a check's
    `headers_from_env`): they are sent and never returned, printed or stored."""
    request = urllib.request.Request(
        url,
        data=b"" if method == "POST" else None,
        method=method,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
            **(headers or {}),
        },
    )
    trace = Trace()
    opener = urllib.request.build_opener(TracingHTTPSHandler(context or tls_context(), trace))
    try:
        with opener.open(request, timeout=timeout) as resp:  # noqa: S310 (https only, validated in config)
            body = resp.read(MAX_BODY)
            status = resp.status
            headers = lower_headers(resp.headers)
    except urllib.error.HTTPError as exc:
        body = exc.read(MAX_BODY) if exc.fp else b""
        status = exc.code
        headers = lower_headers(exc.headers)
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        reason = getattr(exc, "reason", exc)
        return {
            "status": None,
            "headers": trace.headers,
            "body": b"",
            "latency_ms": None,
            "error": describe_error(reason),
            "stage": trace.stage,
            "exception": reason,
            "answered": trace.answered,
            "timings": trace.timings(),
        }
    return {
        "status": status,
        "headers": headers,
        "body": body,
        "latency_ms": int((trace.clock() - trace.started) * 1000),
        "error": None,
        "stage": None,
        "exception": None,
        "answered": True,
        "timings": trace.timings(),
    }


def describe_error(reason) -> str:
    if isinstance(reason, ssl.SSLCertVerificationError):
        return f"TLS certificate invalid ({reason.verify_message})"
    if isinstance(reason, ssl.SSLError):
        return "TLS handshake failed"
    if isinstance(reason, socket.gaierror):
        return "DNS lookup failed"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "timed out"
    if isinstance(reason, ConnectionRefusedError):
        return "connection refused"
    if isinstance(reason, ConnectionResetError):
        return "connection reset"
    return str(reason)[:200] or reason.__class__.__name__


def tls_days_left(host: str, timeout: float, now: datetime | None = None) -> int | None:
    """Days until the leaf certificate expires, or None if it cannot be read (the GET itself
    reports invalid certificates; this only feeds the expiry warning)."""
    context = tls_context()
    try:
        with socket.create_connection((host, 443), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
    except (OSError, ssl.SSLError):
        return None
    not_after = cert.get("notAfter") if cert else None
    if not not_after:
        return None
    expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(not_after), tz=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return int((expires - now).total_seconds() // 86400)


def needs_retry(response: dict) -> bool:
    return bool(response["error"] or response["status"] is None or response["status"] >= 500)


def probe_all(
    checks: list[dict],
    timeout: float,
    retry_delay: float,
    fetcher=fetch,
    tls_probe=tls_days_left,
    headers: list[dict | None] | None = None,
) -> list[dict]:
    """Probe every check (in parallel); a failed probe is retried once after `retry_delay`
    so that a single dropped packet on the runner does not count as downtime. The response of
    the retry carries the first attempt under `previous`, so that both can be reported.

    A check's own `timeout_seconds` replaces `timeout`. `headers` lines up with `checks`: the
    resolved extra request headers of each check (its `headers_from_env`), or None."""
    extra = headers or [None] * len(checks)

    def one(item: tuple[dict, dict | None]) -> dict:
        check, hdrs = item
        limit = check.get("timeout_seconds", timeout)
        method = check.get("method", "GET")
        options = {"method": method, "headers": hdrs} if method != "GET" or hdrs else {}

        def attempt() -> dict:
            return fetcher(check["url"], limit, **options)

        response = attempt()
        if needs_retry(response):
            time.sleep(retry_delay)
            retry = attempt()
            retry["previous"] = response
            return retry
        return response

    hosts = sorted({urlsplit(c["url"]).hostname for c in checks})
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(one, zip(checks, extra)))
        tls = dict(zip(hosts, pool.map(lambda h: tls_probe(h, timeout), hosts)))
    for check, response in zip(checks, responses):
        response["tls_days_left"] = tls.get(urlsplit(check["url"]).hostname)
    return responses
