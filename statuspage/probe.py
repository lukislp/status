"""The only module that talks to the network: one GET per check, plus one TLS handshake per
host to read the certificate expiry. Standard library only."""

from __future__ import annotations

import socket
import ssl
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlsplit

USER_AGENT = "lktec-status/1.0 (+https://status.lktec.org)"
MAX_BODY = 2 * 1024 * 1024


def fetch(url: str, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"},
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310 (https only, validated in config)
            body = resp.read(MAX_BODY)
            status = resp.status
            headers = {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        body = exc.read(MAX_BODY) if exc.fp else b""
        status = exc.code
        headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        return {"status": None, "headers": {}, "body": b"", "latency_ms": None, "error": describe_error(reason)}
    latency = int((time.monotonic() - started) * 1000)
    return {"status": status, "headers": headers, "body": body, "latency_ms": latency, "error": None}


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
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
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


def probe_all(checks: list[dict], timeout: float, retry_delay: float) -> list[dict]:
    """Probe every check (in parallel); a failed probe is retried once after `retry_delay`
    so that a single dropped packet on the runner does not count as downtime."""

    def one(check: dict) -> dict:
        response = fetch(check["url"], timeout)
        if response["error"] or response["status"] is None or response["status"] >= 500:
            time.sleep(retry_delay)
            response = fetch(check["url"], timeout)
        return response

    hosts = sorted({urlsplit(c["url"]).hostname for c in checks})
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(one, checks))
        tls = dict(zip(hosts, pool.map(lambda h: tls_days_left(h, timeout), hosts)))
    for check, response in zip(checks, responses):
        response["tls_days_left"] = tls.get(urlsplit(check["url"]).hostname)
    return responses
