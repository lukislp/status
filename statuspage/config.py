"""Loading and validating config.json.

Validation is strict on purpose: a typo in a component id or an unknown key would otherwise
surface as a silently missing bar on the live page instead of a red CI run.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

DEFAULT_SETTINGS = {
    "timeout_seconds": 15,
    "retry_delay_seconds": 5,
    "failure_threshold": 2,
    "recovery_threshold": 2,
    "slow_ms": 5000,
    "tls_warn_days": 7,
    "retention_days": 90,
    "publish_interval_minutes": 30,
}

SITE_KEYS = {
    "title",
    "base_url",
    "owner_name",
    "home_url",
    "imprint_url",
    "privacy_url",
    "imprint_url_de",
    "privacy_url_de",
    "repo_url",
}
# `name_de` / `description_de` are the German page's wording; the plain keys are the English one
# (and the fallback when a German text is missing).
COMPONENT_KEYS = {"id", "name", "name_de", "description", "description_de", "planned", "checks"}
TEXT_KEYS = ("name", "name_de", "description", "description_de")
CHECK_KEYS = {
    "name",
    "url",
    "expect_status",
    "expect_header",
    "expect_body_contains",
    "expect_json",
    "maintenance_response",
    "method",
    "headers_from_env",
    "interval_minutes",
    "timeout_seconds",
    "slow_ms",
    "degraded_json",
    "degraded_note",
    "report_json_fields",
}
METHODS = {"GET", "POST"}
# Every run is about five minutes apart; a shorter interval would mean "every run".
MIN_INTERVAL_MINUTES = 5
MAINTENANCE_RESPONSE_KEYS = {"status", "body_contains", "header"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")


class ConfigError(ValueError):
    pass


def load_config(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    return validate_config(raw)


def validate_config(raw: dict) -> dict:
    site = raw.get("site") or {}
    unknown = set(site) - SITE_KEYS
    if unknown:
        raise ConfigError(f"unknown site keys: {sorted(unknown)}")
    for key in ("title", "base_url"):
        if not site.get(key):
            raise ConfigError(f"site.{key} is required")

    settings = dict(DEFAULT_SETTINGS)
    for key, value in (raw.get("settings") or {}).items():
        if key not in DEFAULT_SETTINGS:
            raise ConfigError(f"unknown setting: {key}")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise ConfigError(f"setting {key} must be a non-negative number")
        settings[key] = value
    if settings["failure_threshold"] < 1 or settings["recovery_threshold"] < 1:
        raise ConfigError("failure_threshold and recovery_threshold must be at least 1")

    groups = raw.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ConfigError("at least one group is required")
    seen: set[str] = set()
    for group in groups:
        if not group.get("name"):
            raise ConfigError("every group needs a name")
        for comp in group.get("components") or []:
            _validate_component(comp, seen)
        if not group.get("components"):
            raise ConfigError(f"group {group['name']!r} has no components")
    issues = raw.get("issues") or {}
    unknown = set(issues) - {"repo", "authors"}
    if unknown:
        raise ConfigError(f"unknown issues keys: {sorted(unknown)}")
    authors = issues.get("authors") or []
    if not isinstance(authors, list) or not all(isinstance(a, str) and a for a in authors):
        raise ConfigError("issues.authors must be a list of GitHub logins")
    resets = raw.get("uptime_resets") or []
    if not isinstance(resets, list) or not all(isinstance(d, str) and DAY_RE.match(d) for d in resets):
        raise ConfigError("uptime_resets must be a list of YYYY-MM-DD days")
    return {
        "site": site,
        "settings": settings,
        "groups": groups,
        "issues": {"repo": issues.get("repo", ""), "authors": authors},
        "uptime_resets": resets,
    }


def _validate_component(comp: dict, seen: set[str]) -> None:
    cid = comp.get("id", "")
    if not ID_RE.match(cid):
        raise ConfigError(f"invalid component id {cid!r} (lowercase letters, digits, dashes)")
    if cid in seen:
        raise ConfigError(f"duplicate component id {cid!r}")
    seen.add(cid)
    unknown = set(comp) - COMPONENT_KEYS
    if unknown:
        raise ConfigError(f"component {cid}: unknown keys {sorted(unknown)}")
    if not comp.get("name"):
        raise ConfigError(f"component {cid}: name is required")
    for key in TEXT_KEYS:
        if key in comp and (not isinstance(comp[key], str) or not comp[key].strip()):
            raise ConfigError(f"component {cid}: {key} must be a non-empty string")
    checks = comp.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ConfigError(f"component {cid}: at least one check is required")
    for check in checks:
        unknown = set(check) - CHECK_KEYS
        if unknown:
            raise ConfigError(f"component {cid}: unknown check keys {sorted(unknown)}")
        url = check.get("url", "")
        if not url.startswith("https://"):
            raise ConfigError(f"component {cid}: check url must start with https://")
        status = check.get("expect_status", 200)
        if isinstance(status, int):
            status = [status]
        if not isinstance(status, list) or not all(isinstance(s, int) for s in status):
            raise ConfigError(f"component {cid}: expect_status must be an int or a list of ints")
        _validate_maintenance_response(cid, check.get("maintenance_response"))
        _validate_request_options(cid, check)
    named = [c["name"] for c in checks if c.get("interval_minutes")]
    for name in named:
        if sum(1 for c in checks if c.get("name") == name) != 1:
            raise ConfigError(f"component {cid}: a check with interval_minutes needs a unique name")


def _positive_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _validate_request_options(cid: str, check: dict) -> None:
    """The options of a check that is not a plain GET every run: method, secret headers, its own
    interval, timeout and slow threshold, a degraded answer and the fields printed to the log."""
    method = check.get("method", "GET")
    if method not in METHODS:
        raise ConfigError(f"component {cid}: method must be one of {sorted(METHODS)}")
    headers = check.get("headers_from_env")
    if headers is not None:
        if not isinstance(headers, dict) or not headers:
            raise ConfigError(f"component {cid}: headers_from_env must be a non-empty object")
        for name, env in headers.items():
            if not HEADER_NAME_RE.match(name) or not isinstance(env, str) or not ENV_NAME_RE.match(env):
                raise ConfigError(
                    f"component {cid}: headers_from_env maps a header name to an environment variable (UPPER_CASE)"
                )
    interval = check.get("interval_minutes")
    if interval is not None:
        if not _positive_number(interval) or interval < MIN_INTERVAL_MINUTES:
            raise ConfigError(f"component {cid}: interval_minutes must be at least {MIN_INTERVAL_MINUTES}")
        if not check.get("name"):
            raise ConfigError(f"component {cid}: a check with interval_minutes needs a name")
    for key in ("timeout_seconds", "slow_ms"):
        if key in check and not _positive_number(check[key]):
            raise ConfigError(f"component {cid}: {key} must be a positive number")
    degraded = check.get("degraded_json")
    if degraded is not None and (not isinstance(degraded, dict) or not degraded):
        raise ConfigError(f"component {cid}: degraded_json must be a non-empty object")
    if "degraded_note" in check and (not isinstance(check["degraded_note"], str) or not check["degraded_note"]):
        raise ConfigError(f"component {cid}: degraded_note must be a non-empty string")
    fields = check.get("report_json_fields")
    if fields is not None and (not isinstance(fields, list) or not all(isinstance(f, str) and f for f in fields)):
        raise ConfigError(f"component {cid}: report_json_fields must be a list of field names")


def _validate_maintenance_response(cid: str, spec) -> None:
    """A known, planned response (e.g. a demo reset page) that counts as up. It needs a status
    and something specific to that page, so a bare 503 from anywhere never matches."""
    if spec is None:
        return
    if not isinstance(spec, dict) or set(spec) - MAINTENANCE_RESPONSE_KEYS:
        raise ConfigError(f"component {cid}: maintenance_response keys are {sorted(MAINTENANCE_RESPONSE_KEYS)}")
    if not isinstance(spec.get("status"), int) or isinstance(spec.get("status"), bool):
        raise ConfigError(f"component {cid}: maintenance_response.status must be an int")
    if not spec.get("body_contains") and not spec.get("header"):
        raise ConfigError(f"component {cid}: maintenance_response needs body_contains or header")
    if "header" in spec and not isinstance(spec["header"], dict):
        raise ConfigError(f"component {cid}: maintenance_response.header must be an object")


def issue_labels(config: dict) -> list[tuple[str, str]]:
    """Every label the issue workflow understands, with a description (README + `labels`)."""
    labels = [
        ("incident", "Shown on the status page as an incident"),
        ("maintenance", "Shown on the status page as planned maintenance"),
        ("impact:minor", "Incident impact: minor"),
        ("impact:major", "Incident impact: major"),
        ("impact:critical", "Incident impact: critical"),
    ]
    labels += [(f"component:{c['id']}", f"Affects {c['name']}") for c in all_components(config)]
    return labels


def all_components(config: dict) -> list[dict]:
    return [c for g in config["groups"] for c in g["components"]]


def localized(comp: dict, key: str, lang: str) -> str:
    """A component's `name` or `description` in `lang`: the German page uses `<key>_de` when
    set, everything else (and a missing German text) falls back to the English `<key>`."""
    if lang == "de" and comp.get(f"{key}_de"):
        return comp[f"{key}_de"]
    return comp.get(key) or ""
