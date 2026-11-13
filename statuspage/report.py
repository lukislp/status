"""Formatting of the workflow log: one line per component, with where a check failed and
Cloudflare's request id. Pure text building, no decisions and no I/O.

This is log output only. `cf-ray` ids never go into data/ or the published site; the model
publishes just the phase (see model.where_failed).
"""

from __future__ import annotations

from . import model

NO_RAY_NO_RESPONSE = "no response, so no cf-ray"
NO_RAY_IN_RESPONSE = "response without a cf-ray header"


def timing_text(diag: dict) -> str:
    labels = (("connect_ms", "connect"), ("tls_ms", "tls"), ("ttfb_ms", "first byte"), ("total_ms", "total"))
    return ", ".join(f"{label} {diag[key]} ms" for key, label in labels if diag.get(key) is not None)


def edge_text(diag: dict) -> str:
    if not diag.get("answered"):
        return NO_RAY_NO_RESPONSE
    ids = [f"cf-ray {diag['cf_ray']}"] if diag.get("cf_ray") else []
    if diag.get("cf_cache_status"):
        ids.append(f"cf-cache-status {diag['cf_cache_status']}")
    return ", ".join(ids) or NO_RAY_IN_RESPONSE


def attempt_text(diag: dict, with_phase: bool = True) -> str:
    """`phase: timeout_response, connect 43 ms, tls 61 ms, total 15062 ms; no response, so no cf-ray`"""
    head = [f"phase: {diag['phase']}"] if with_phase and diag.get("phase") else []
    timings = timing_text(diag)
    left = ", ".join(head + ([timings] if timings else []))
    return f"{left}; {edge_text(diag)}" if left else edge_text(diag)


def check_detail(check: dict) -> str:
    """The parenthesised part after a check's problem text, for one or two attempts."""
    if check.get("carried_from"):
        return f"result of {check['carried_from']}"
    attempts = check.get("attempts") or ([check["diag"]] if check.get("diag") else [])
    if len(attempts) > 1:
        return "; ".join(f"attempt {n}: {attempt_text(a)}" for n, a in enumerate(attempts, 1))
    return attempt_text(attempts[0]) if attempts else ""


def check_text(check: dict) -> str | None:
    problem = model.check_problem(check)
    if problem is None:
        return None
    detail = check_detail(check)
    return f"{problem} ({detail})" if detail else problem


def ok_text(checks: list[dict], latency_ms: int | None) -> str:
    """A healthy component: the latency, and for each check the request id of its response."""
    text = f"{latency_ms} ms" if latency_ms is not None else ""
    notes = []
    for check in checks:
        diag = check.get("diag")
        if diag and diag.get("answered"):
            ids = edge_text(diag)
            notes.append(f"{check['name']}: {ids}" if len(checks) > 1 else ids)
    return f"{text} ({'; '.join(notes)})" if notes else text


def component_line(component_id: str, state: dict, checks: list[dict] | None) -> str:
    """`<id> <status> <detail>` as the check workflow has always printed it, extended with the
    phase and request ids. `checks` are this run's results (None when nothing was probed)."""
    problems = [t for t in map(check_text, checks or []) if t]
    if problems:
        detail = "; ".join(problems)
    elif checks:
        detail = ok_text(checks, state.get("latency_ms"))
    else:
        latency = state.get("latency_ms")
        detail = state.get("message") or (f"{latency} ms" if latency is not None else "")
    return f"{component_id:<20} {state.get('status', '-'):<12} {detail}"


def outcome_text(check: dict) -> str:
    """`ok`, `degraded: <note>` or `failed: <error>`, for a check with its own interval."""
    if not check["ok"]:
        return f"failed: {check['error']}"
    if check.get("degraded"):
        return f"degraded: {check.get('degraded_note') or 'reports a degraded state'}"
    return "slow" if check.get("slow") else "ok"


def scheduled_line(component_id: str, check: dict, next_due: str | None = None) -> str:
    """One extra log line for a check with its own interval: the outcome of this run's probe
    (HTTP status, latency and the JSON fields named in `report_json_fields`), or which earlier
    result is carried forward and when the next probe is due. Never a request header."""
    name = check["name"]
    if check.get("carried_from"):
        due = f", next probe at {next_due}" if next_due else ""
        return f"{component_id:<20} {'':<12} {name}: {outcome_text(check)} (probed {check['carried_from']}{due})"
    parts = [f"HTTP {check['http_status']}" if check.get("http_status") is not None else "no response"]
    if check.get("latency_ms") is not None:
        parts.append(f"{check['latency_ms']} ms")
    fields = check.get("report") or {}
    if fields:
        parts.append(", ".join(f"{k}={v}" for k, v in fields.items()))
    return f"{component_id:<20} {'':<12} {name}: probed now, {outcome_text(check)} ({'; '.join(parts)})"


def skipped_line(component_id: str, check: dict, missing: list[str]) -> str:
    """A GitHub Actions warning annotation for a check skipped because a secret is not set
    (variable names only, never a value)."""
    return (
        f"::warning::{component_id}: check {check.get('name') or check['url']!r} skipped, "
        f"{', '.join(missing)} not set"
    )
