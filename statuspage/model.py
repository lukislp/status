"""Pure status logic: judging a probe, classifying a component, rolling results up per day,
opening and resolving automatic incidents, and deciding when a run is worth publishing.

Nothing in here touches the network or the clock; callers pass `now` in. That keeps every rule
on this page testable without a single real request.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

from .config import all_components

# Component statuses, least to most severe. "maintenance" ranks below "degraded" so that a real
# problem during a maintenance window still shows as the problem.
SEVERITY = ["operational", "maintenance", "degraded", "partial", "major"]
STATUS_LABELS = {
    "operational": "Running",
    "maintenance": "Maintenance",
    "degraded": "Slow",
    "partial": "Partly down",
    "major": "Down",
    "planned": "Coming soon",
    "unknown": "No data yet",
}
BANNER_LABELS = {
    "operational": "Everything is up and running",
    "maintenance": "Planned maintenance is underway",
    "degraded": "Some services are slower than usual",
    "partial": "Some services are partly unavailable",
    "major": "A service is currently down",
}
# Incident impact <-> component status.
IMPACT_FOR_STATUS = {"degraded": "minor", "partial": "major", "major": "critical"}
STATUS_FOR_IMPACT = {
    "none": "operational",
    "minor": "degraded",
    "major": "partial",
    "critical": "major",
    "maintenance": "maintenance",
}
IMPACTS = ["none", "maintenance", "minor", "major", "critical"]


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def worst(statuses) -> str:
    ranked = [s for s in statuses if s in SEVERITY]
    if not ranked:
        return "operational"
    return max(ranked, key=SEVERITY.index)


def worst_impact(impacts) -> str:
    ranked = [i for i in impacts if i in IMPACTS]
    if not ranked:
        return "none"
    return max(ranked, key=IMPACTS.index)


# --------------------------------------------------------------------------- single probe


def evaluate_check(check: dict, response: dict, settings: dict) -> dict:
    """Judge one HTTP response against a check's expectations.

    `response` keys: status (int or None when no response arrived), headers (lower-cased
    names), body (bytes), latency_ms, error (transport error text or None), tls_days_left.
    """
    result = {
        "name": check.get("name") or check["url"],
        "ok": False,
        "slow": False,
        "tls_warning": False,
        "latency_ms": response.get("latency_ms"),
        "http_status": response.get("status"),
        "tls_days_left": response.get("tls_days_left"),
        "error": None,
    }
    if response.get("error"):
        result["error"] = response["error"]
        return result

    expected = check.get("expect_status", 200)
    expected = [expected] if isinstance(expected, int) else expected
    if response.get("status") not in expected:
        result["error"] = f"HTTP {response.get('status')}"
        return result

    headers = response.get("headers") or {}
    for name, value in (check.get("expect_header") or {}).items():
        actual = headers.get(name.lower())
        if actual is None:
            result["error"] = f"missing header {name}"
            return result
        if str(actual).strip() != str(value):
            result["error"] = f"header {name} is {actual!r}, expected {value!r}"
            return result

    body = response.get("body") or b""
    text = body.decode("utf-8", errors="replace")
    needle = check.get("expect_body_contains")
    if needle and needle not in text:
        result["error"] = "expected content missing from the response"
        return result

    expected_json = check.get("expect_json")
    if expected_json:
        try:
            doc = json.loads(text)
        except ValueError:
            result["error"] = "response is not valid JSON"
            return result
        if not isinstance(doc, dict):
            result["error"] = "JSON response is not an object"
            return result
        for key, value in expected_json.items():
            if doc.get(key) != value:
                result["error"] = f"JSON field {key!r} is {doc.get(key)!r}, expected {value!r}"
                return result

    result["ok"] = True
    latency = response.get("latency_ms")
    if latency is not None and latency > settings["slow_ms"]:
        result["slow"] = True
    days = response.get("tls_days_left")
    if days is not None and days < settings["tls_warn_days"]:
        result["tls_warning"] = True
    return result


# --------------------------------------------------------------------------- component


def classify_component(checks: list[dict], fail_streak: int, threshold: int) -> str:
    """Component status from this run's check results and its consecutive failed runs
    (including this one). A single failed run is reported as degraded, not as an outage:
    runners and networks blip, and one blip should not paint the page red."""
    failed = [c for c in checks if not c["ok"]]
    if failed:
        if fail_streak < threshold:
            return "degraded"
        return "major" if len(failed) == len(checks) else "partial"
    if any(c["slow"] or c["tls_warning"] for c in checks):
        return "degraded"
    return "operational"


def component_message(checks: list[dict]) -> str:
    problems = []
    for c in checks:
        if not c["ok"]:
            problems.append(f"{c['name']}: {c['error']}")
        elif c["slow"]:
            problems.append(f"{c['name']}: slow response ({c['latency_ms']} ms)")
        elif c["tls_warning"]:
            problems.append(f"{c['name']}: TLS certificate expires in {c['tls_days_left']} days")
    return "; ".join(problems)


# --------------------------------------------------------------------------- data files


def empty_data() -> dict:
    return {
        "status": {"generated_at": None, "published_at": None, "components": {}},
        "history": {"days": {}},
        "incidents": {"incidents": []},
    }


def record_day(history: dict, component_id: str, day: str, checks: list[dict]) -> None:
    days = history["days"].setdefault(component_id, {})
    entry = days.setdefault(day, {"n": 0, "up": 0.0, "fail": 0, "lat_sum": 0, "lat_n": 0, "lat_max": 0})
    ok = sum(1 for c in checks if c["ok"])
    entry["n"] += 1
    entry["up"] = round(entry["up"] + ok / len(checks), 4)
    if ok < len(checks):
        entry["fail"] += 1
    primary = checks[0]
    if primary["ok"] and primary["latency_ms"] is not None:
        entry["lat_sum"] += int(primary["latency_ms"])
        entry["lat_n"] += 1
        entry["lat_max"] = max(entry["lat_max"], int(primary["latency_ms"]))


def day_uptime(entry: dict | None) -> float | None:
    if not entry or not entry.get("n"):
        return None
    return 100.0 * entry["up"] / entry["n"]


def range_uptime(days: dict, day_keys: list[str]) -> float | None:
    n = sum(days[d]["n"] for d in day_keys if d in days)
    if not n:
        return None
    up = sum(days[d]["up"] for d in day_keys if d in days)
    return 100.0 * up / n


def trim(data: dict, now: datetime, retention_days: int, known_ids: set[str]) -> None:
    """Bound the repository: drop days and resolved automatic incidents older than the
    retention window, and history of components that were removed from the config."""
    cutoff = (now - timedelta(days=retention_days)).strftime("%Y-%m-%d")
    days = data["history"]["days"]
    for cid in list(days):
        if cid not in known_ids:
            del days[cid]
            continue
        for day in list(days[cid]):
            if day < cutoff:
                del days[cid][day]
    for cid in list(data["status"]["components"]):
        if cid not in known_ids:
            del data["status"]["components"][cid]
    cutoff_iso = iso(now - timedelta(days=retention_days))
    data["incidents"]["incidents"] = [
        i
        for i in data["incidents"]["incidents"]
        if not i.get("resolved_at") or i["resolved_at"] >= cutoff_iso
    ]


# --------------------------------------------------------------------------- one run


def apply_run(data: dict, config: dict, results: dict[str, list[dict]], now: datetime) -> tuple[dict, dict]:
    """Fold one checker run into the data files.

    `results` maps component id -> evaluated check results (planned components are absent).
    Returns the new data and a summary {"changed": [...ids whose status changed],
    "opened": [...incident ids], "resolved": [...incident ids]}.
    """
    data = copy.deepcopy(data)
    settings = config["settings"]
    threshold = int(settings["failure_threshold"])
    recovery = int(settings["recovery_threshold"])
    stamp = iso(now)
    day = now.strftime("%Y-%m-%d")
    events = {"changed": [], "opened": [], "resolved": []}
    incidents = data["incidents"]["incidents"]
    by_id = {i["id"]: i for i in incidents}

    for group in config["groups"]:
        for comp in group["components"]:
            cid = comp["id"]
            prev = data["status"]["components"].get(cid, {})
            if comp.get("planned"):
                state = {"status": "planned", "checked_at": None, "checks": []}
                if prev.get("status") != "planned":
                    events["changed"].append(cid)
                data["status"]["components"][cid] = state
                continue
            checks = results.get(cid)
            if not checks:
                continue
            failed_run = any(not c["ok"] for c in checks)
            fail_streak = prev.get("fail_streak", 0) + 1 if failed_run else 0
            ok_streak = 0 if failed_run else prev.get("ok_streak", 0) + 1
            first_fail_at = (prev.get("first_fail_at") or stamp) if failed_run else None
            status = classify_component(checks, fail_streak, threshold)
            message = component_message(checks)
            open_id = prev.get("open_incident")
            if open_id and open_id not in by_id:
                open_id = None

            if failed_run and fail_streak >= threshold and not open_id:
                incident = _open_auto_incident(comp, status, message, first_fail_at, stamp)
                incidents.append(incident)
                by_id[incident["id"]] = incident
                open_id = incident["id"]
                events["opened"].append(open_id)
            elif open_id and failed_run:
                _escalate_auto_incident(by_id[open_id], status, message, stamp)
            elif open_id and ok_streak >= recovery:
                _resolve_auto_incident(by_id[open_id], comp, stamp)
                events["resolved"].append(open_id)
                open_id = None

            if prev.get("status") != status:
                events["changed"].append(cid)
            data["status"]["components"][cid] = {
                "status": status,
                "checked_at": stamp,
                "latency_ms": checks[0]["latency_ms"] if checks[0]["ok"] else None,
                "message": message,
                "fail_streak": fail_streak,
                "ok_streak": ok_streak,
                "first_fail_at": first_fail_at,
                "open_incident": open_id,
                "checks": [
                    {k: c[k] for k in ("name", "ok", "latency_ms", "http_status", "tls_days_left", "error")}
                    for c in checks
                ],
            }
            record_day(data["history"], cid, day, checks)

    data["status"]["generated_at"] = stamp
    trim(data, now, int(settings["retention_days"]), {c["id"] for c in all_components(config)})
    return data, events


def _open_auto_incident(comp: dict, status: str, message: str, started: str, stamp: str) -> dict:
    compact = started.replace("-", "").replace(":", "").replace("T", "-")[:13]
    verb = "is unavailable" if status == "major" else "is partially unavailable"
    return {
        "id": f"auto-{comp['id']}-{compact}",
        "title": f"{comp['name']} {verb}",
        "components": [comp["id"]],
        "impact": IMPACT_FOR_STATUS.get(status, "major"),
        "auto": True,
        "created_at": started,
        "resolved_at": None,
        "updates": [
            {
                "status": "investigating",
                "at": stamp,
                "body": f"Automated checks for {comp['name']} are failing. {message}".strip(),
            }
        ],
    }


def _escalate_auto_incident(incident: dict, status: str, message: str, stamp: str) -> None:
    impact = IMPACT_FOR_STATUS.get(status)
    if impact and IMPACTS.index(impact) > IMPACTS.index(incident["impact"]):
        incident["impact"] = impact
        incident["updates"].append(
            {"status": "investigating", "at": stamp, "body": f"The problem got worse. {message}".strip()}
        )


def _resolve_auto_incident(incident: dict, comp: dict, stamp: str) -> None:
    minutes = int((parse_iso(stamp) - parse_iso(incident["created_at"])).total_seconds() // 60)
    incident["resolved_at"] = stamp
    incident["updates"].append(
        {
            "status": "resolved",
            "at": stamp,
            "body": f"Automated checks for {comp['name']} are passing again. "
            f"The component was affected for about {format_duration(minutes)}.",
        }
    )


def format_duration(minutes: int) -> str:
    if minutes < 60:
        return f"{max(minutes, 1)} min"
    hours, rest = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {rest} min" if rest else f"{hours} h"
    return f"{hours // 24} days"


def should_publish(events: dict, published_at: str | None, now: datetime, interval_minutes: float) -> bool:
    """Commit and deploy when something visible changed, otherwise at most once per interval.
    Runs in between only update the cached working copy (see the check workflow)."""
    if events["changed"] or events["opened"] or events["resolved"]:
        return True
    if not published_at:
        return True
    return now - parse_iso(published_at) >= timedelta(minutes=interval_minutes)
