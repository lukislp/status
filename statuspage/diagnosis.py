"""Where a check failed: pure classification, no network and no clock.

The probe (probe.py) records how far a request got and which exception stopped it; this module
turns that into one small value, the *phase*, in a single function (`classify_phase`). The
model decides whether a check is ok, the phase only explains a failed or slow one.

| phase              | meaning                                                              |
| ------------------ | -------------------------------------------------------------------- |
| `dns`              | the name did not resolve                                             |
| `connect`          | TCP connection refused, unreachable or timed out                     |
| `tls`              | handshake or certificate failed (also a timeout during the handshake)|
| `timeout_response` | connected, but no response headers within the timeout                |
| `response_error`   | connected, but the connection broke or the answer was unusable       |
| `http_status`      | the server answered with an unexpected status                        |
| `body`             | the answer lacked the expected header, text or JSON field            |
| `slow`             | the answer was fine but slower than `slow_ms`                        |
"""

from __future__ import annotations

import re
import socket
import ssl

DNS = "dns"
CONNECT = "connect"
TLS = "tls"
TIMEOUT_RESPONSE = "timeout_response"
RESPONSE_ERROR = "response_error"
HTTP_STATUS = "http_status"
BODY = "body"
SLOW = "slow"
PHASES = (DNS, CONNECT, TLS, TIMEOUT_RESPONSE, RESPONSE_ERROR, HTTP_STATUS, BODY, SLOW)

# How far a request got when the transport failed (set by the probe).
STAGE_CONNECT = "connect"  # resolving the name and opening the TCP connection
STAGE_TLS = "tls"  # TLS handshake
STAGE_RESPONSE = "response"  # request sent, waiting for the response headers
STAGE_BODY = "body"  # headers received, reading the body

# Why a check failed, as decided by the model.
FAIL_TRANSPORT = "transport"
FAIL_STATUS = "status"
FAIL_BODY = "body"

# Wording for the public incident text (what the phase means, no internals).
PHASE_TEXT = {
    DNS: "the name could not be resolved",
    CONNECT: "no connection could be opened",
    TLS: "the secure connection could not be set up",
    TIMEOUT_RESPONSE: "connected, but no response arrived in time",
    RESPONSE_ERROR: "connected, but the connection broke before a complete response",
    HTTP_STATUS: "the server answered with an unexpected status",
    BODY: "the server answered, but not with the expected content",
    SLOW: "the server answered, but slowly",
}

# Only these two response headers are ever recorded, and only in this shape.
_EDGE_VALUE = re.compile(r"[A-Za-z0-9._:-]{1,64}")


def classify_phase(failure: str | None, stage: str | None, exc: BaseException | None, slow: bool) -> str | None:
    """The one place that decides the phase.

    `failure` is the model's verdict (None for an ok check, otherwise FAIL_*), `stage` how far
    the transport got and `exc` what stopped it (both only matter for FAIL_TRANSPORT), `slow`
    whether an ok check was slower than `slow_ms`. An ok, fast check has no phase.
    """
    if failure is None:
        return SLOW if slow else None
    if failure == FAIL_STATUS:
        return HTTP_STATUS
    if failure == FAIL_BODY:
        return BODY
    if isinstance(exc, socket.gaierror):
        return DNS
    if isinstance(exc, ssl.SSLError):
        return TLS
    if stage == STAGE_TLS:
        return TLS
    if stage == STAGE_RESPONSE:
        return TIMEOUT_RESPONSE if isinstance(exc, (TimeoutError, socket.timeout)) else RESPONSE_ERROR
    if stage == STAGE_BODY:
        return RESPONSE_ERROR
    return CONNECT


def edge_ids(headers: dict | None) -> dict:
    """Cloudflare's request id (`cf-ray`) and cache status, if the response carried them.
    Headers are lower-cased by the probe. A value that does not look like an id is dropped, so
    nothing but a short token can ever reach a log line."""
    found = {}
    for key, name in (("cf_ray", "cf-ray"), ("cf_cache_status", "cf-cache-status")):
        value = str((headers or {}).get(name, "")).strip()
        found[key] = value if _EDGE_VALUE.fullmatch(value) else None
    return found


def diagnose(phase: str | None, timings: dict | None, headers: dict | None, answered: bool) -> dict:
    """The diagnostic record of one attempt. `answered` is True when response headers arrived."""
    timings = timings or {}
    return {
        "phase": phase,
        "answered": answered,
        **{key: timings.get(key) for key in ("connect_ms", "tls_ms", "ttfb_ms", "total_ms")},
        **edge_ids(headers),
    }
