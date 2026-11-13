"""Which checks this run probes. Pure: callers pass the previous state, the time and which
environment variables are set.

Most checks are probed on every run. A check with `interval_minutes` (for example the chat
assistant's end-to-end probe, which costs a model call) is probed only when its last result is at
least that old, and in the runs in between its last result is carried forward, so the component
keeps showing it. A check without a previous result is due at once, so a new check reports in the
first run after it was added. A check whose `headers_from_env` names an unset variable is skipped
with a warning: the rest of the component is still checked.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .model import parse_iso

PROBE = "probe"
CARRY = "carry"
SKIP = "skip"


def missing_env(check: dict, environ) -> list[str]:
    """The environment variables a check's headers need that are not set (names only)."""
    return [name for name in (check.get("headers_from_env") or {}).values() if not environ.get(name)]


def previous_result(state: dict | None, check: dict) -> dict | None:
    """The stored result of this check from data/status.json, by name, if it has a timestamp."""
    for entry in (state or {}).get("checks") or []:
        if entry.get("name") == check.get("name") and entry.get("checked_at"):
            return entry
    return None


def next_due(check: dict, previous: dict | None) -> datetime | None:
    if not check.get("interval_minutes") or not previous:
        return None
    try:
        return parse_iso(previous["checked_at"]) + timedelta(minutes=check["interval_minutes"])
    except (KeyError, ValueError):
        return None


def plan_check(check: dict, state: dict | None, now: datetime, environ) -> tuple[str, dict | None]:
    """(PROBE | CARRY | SKIP, the previous stored result for CARRY)."""
    if missing_env(check, environ):
        return SKIP, None
    if not check.get("interval_minutes"):
        return PROBE, None
    previous = previous_result(state, check)
    due = next_due(check, previous)
    if due is None or now >= due:
        return PROBE, None
    return CARRY, previous


def carried_result(previous: dict) -> dict:
    """An evaluated check result (model.evaluate_check's shape) rebuilt from the stored one, so
    the status rules treat it like a fresh result. `carried_from` is when it was really probed."""
    return {
        "name": previous["name"],
        "ok": bool(previous.get("ok")),
        "slow": bool(previous.get("slow")),
        "tls_warning": bool(previous.get("tls_warning")),
        "degraded": bool(previous.get("degraded")),
        "degraded_note": previous.get("degraded_note"),
        "latency_ms": previous.get("latency_ms"),
        "http_status": previous.get("http_status"),
        "tls_days_left": previous.get("tls_days_left"),
        "error": previous.get("error"),
        "interval": True,
        "carried_from": previous["checked_at"],
        "diag": {"phase": previous.get("phase"), "answered": False},
        "attempts": [],
    }


def resolve_headers(check: dict, environ) -> dict | None:
    """The extra request headers of a check, with the secret values from the environment.
    Only for the request itself; never printed or stored."""
    mapping = check.get("headers_from_env")
    if not mapping:
        return None
    return {header: environ[name] for header, name in mapping.items()}
