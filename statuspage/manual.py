"""Hand-written incidents (incidents/*.md) and maintenance windows (maintenance/*.md).

Incident file:

    ---
    title: Studylife demo login fails
    components: studylife-demo
    impact: major
    ---

    ## resolved 2026-10-30 18:40
    The fix is live.

    ## investigating 2026-10-30 18:05
    We are looking into failing demo logins.

Maintenance file:

    ---
    title: Demo host kernel update
    components: homelabhub-demo, piwatch-demo
    start: 2026-11-02 02:00
    end: 2026-11-02 03:00
    ---

    ## scheduled 2026-10-30 12:00
    The demos may be unreachable for a few minutes.

Times are UTC. Files whose name starts with "_" are ignored (templates).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from .model import iso

INCIDENT_STATUSES = ["investigating", "identified", "monitoring", "update", "resolved"]
MAINTENANCE_STATUSES = ["scheduled", "in-progress", "verifying", "update", "completed"]
INCIDENT_IMPACTS = ["none", "minor", "major", "critical"]
FRONT_KEYS = {
    "incident": {"title", "components", "impact"},
    "maintenance": {"title", "components", "start", "end"},
}
HEADING_RE = re.compile(
    r"^##\s+([A-Za-z-]+)\s+(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})(?::\d{2})?\s*(?:UTC|Z)?\s*$"
)
STEM_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")


class IncidentError(ValueError):
    pass


def parse_time(value: str, where: str) -> str:
    value = value.strip().removesuffix("UTC").removesuffix("Z").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return iso(datetime.strptime(value, fmt).replace(tzinfo=timezone.utc))
        except ValueError:
            continue
    raise IncidentError(f"{where}: cannot parse time {value!r} (use YYYY-MM-DD HH:MM, UTC)")


def parse_incident(text: str, stem: str, component_ids: set[str], kind: str = "incident") -> dict:
    folder = "maintenance" if kind == "maintenance" else "incidents"
    where = f"{folder}/{stem}.md"
    if not STEM_RE.match(stem):
        raise IncidentError(f"{where}: file name must be lowercase letters, digits and dashes")
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        raise IncidentError(f"{where}: must start with a '---' front matter block")
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise IncidentError(f"{where}: front matter is not closed with '---'") from None
    front: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        key = key.strip()
        if not sep or key not in FRONT_KEYS[kind]:
            hint = " (maintenance windows go into maintenance/)" if key in ("start", "end") or value.strip() == "maintenance" else ""
            raise IncidentError(f"{where}: unknown front matter line {line!r}{hint}")
        front[key] = value.strip()

    title = front.get("title")
    if not title:
        raise IncidentError(f"{where}: title is required")
    if kind == "maintenance":
        impact = "maintenance"
    else:
        impact = front.get("impact", "minor")
        if impact not in INCIDENT_IMPACTS:
            raise IncidentError(f"{where}: impact must be one of {INCIDENT_IMPACTS}")
    components = [c.strip() for c in front.get("components", "").split(",") if c.strip()]
    for cid in components:
        if cid not in component_ids:
            raise IncidentError(f"{where}: unknown component {cid!r}")
    allowed = MAINTENANCE_STATUSES if kind == "maintenance" else INCIDENT_STATUSES

    updates = []
    current = None
    for line in lines[end + 1 :]:
        if line.startswith("## "):
            match = HEADING_RE.match(line.strip())
            if not match:
                raise IncidentError(f"{where}: bad update heading {line!r} (use '## <status> YYYY-MM-DD HH:MM')")
            status = match.group(1).lower()
            if status not in allowed:
                raise IncidentError(f"{where}: status {status!r} must be one of {allowed}")
            current = {
                "status": status,
                "at": parse_time(f"{match.group(2)} {match.group(3)}", where),
                "body": "",
            }
            updates.append(current)
        elif current is not None:
            current["body"] += line + "\n"
        elif line.strip():
            raise IncidentError(f"{where}: text before the first '## <status> <time>' heading")
    for update in updates:
        update["body"] = update["body"].strip()
    if not updates:
        raise IncidentError(f"{where}: at least one update is required")
    updates.sort(key=lambda u: u["at"])

    incident = {
        "id": stem,
        "title": title,
        "components": components,
        "impact": impact,
        "auto": False,
        "created_at": updates[0]["at"],
        "resolved_at": None,
        "updates": updates,
    }
    if updates[-1]["status"] in ("resolved", "completed"):
        incident["resolved_at"] = updates[-1]["at"]
    if kind == "maintenance":
        for key in ("start", "end"):
            if not front.get(key):
                raise IncidentError(f"{where}: {key} is required")
        incident["scheduled_start"] = parse_time(front["start"], where)
        incident["scheduled_end"] = parse_time(front["end"], where)
        if incident["scheduled_end"] <= incident["scheduled_start"]:
            raise IncidentError(f"{where}: end must be after start")
    return incident


def load_incidents(directory: str | Path, component_ids: set[str], kind: str = "incident") -> list[dict]:
    directory = Path(directory)
    if not directory.is_dir():
        return []
    result = []
    for path in sorted(directory.glob("*.md")):
        if path.name.startswith("_") or path.name.lower() == "readme.md":
            continue
        result.append(parse_incident(path.read_text(encoding="utf-8"), path.stem, component_ids, kind))
    return result


def load_manual(incidents_dir: str | Path, maintenance_dir: str | Path, component_ids: set[str]) -> list[dict]:
    items = load_incidents(incidents_dir, component_ids) + load_incidents(maintenance_dir, component_ids, "maintenance")
    seen: set[str] = set()
    for item in items:
        if item["id"] in seen:
            raise IncidentError(f"{item['id']}: the same file name is used in incidents/ and maintenance/")
        seen.add(item["id"])
    return items


def maintenance_state(item: dict, now_iso: str) -> str:
    """'scheduled', 'in-progress', 'verifying' or 'completed' for a maintenance window."""
    # The last update that states a phase decides; plain "update" posts only add text.
    phases = [u["status"] for u in item["updates"] if u["status"] != "update"]
    latest = phases[-1] if phases else "scheduled"
    if latest == "completed" or item.get("resolved_at"):
        return "completed"
    if latest == "scheduled":
        if now_iso >= item["scheduled_end"]:
            return "completed"
        return "scheduled" if now_iso < item["scheduled_start"] else "in-progress"
    return latest


def phase(incident: dict, now_iso: str) -> str:
    """'active', 'upcoming' (maintenance not yet started) or 'past'."""
    if incident["impact"] == "maintenance":
        state = maintenance_state(incident, now_iso)
        return {"completed": "past", "scheduled": "upcoming"}.get(state, "active")
    return "past" if incident.get("resolved_at") else "active"
