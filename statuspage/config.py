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
    "repo_url",
}
COMPONENT_KEYS = {"id", "name", "description", "planned", "checks"}
CHECK_KEYS = {
    "name",
    "url",
    "expect_status",
    "expect_header",
    "expect_body_contains",
    "expect_json",
    "maintenance_response",
}
MAINTENANCE_RESPONSE_KEYS = {"status", "body_contains", "header"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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
