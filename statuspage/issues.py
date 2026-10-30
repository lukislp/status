"""Incidents and maintenance windows from GitHub Issues in the status repository.

Anyone can open an issue on a public repository - and an issue template can even attach a
label on an outsider's behalf - so the trust rule is strict: an issue counts only if its author
is in `issues.authors`, and only comments by those authors become status updates. Everything
else is ignored. Issue text is never rendered as HTML: it goes through the same escape-first
inline markdown as the incident files.

Mapping (documented in the README):
  label `incident` / `maintenance`      what the issue is
  label `component:<id>`                affected component (or the form's checkboxes)
  label `impact:minor|major|critical`   incident impact (or the form's dropdown; default minor)
  issue body                            first update (maintenance: needs start/end)
  comment by an author                  further update; optional leading keyword sets the status
  issue closed (completed)              resolved / completed at closed_at
  issue closed as "not planned"         withdrawn: not shown at all
"""

from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .model import iso

INCIDENT_KEYWORDS = ["investigating", "identified", "monitoring", "update", "resolved"]
MAINTENANCE_KEYWORDS = ["scheduled", "in-progress", "verifying", "update", "completed"]
IMPACTS = ["none", "minor", "major", "critical"]
KEYWORD_RE = re.compile(
    r"^\s*\**\s*(investigating|identified|monitoring|resolved|update|scheduled|in[- ]progress|verifying|completed)"
    r"\s*\**\s*(?:[:–—-]\s*|\n|$)",
    re.IGNORECASE,
)
FIELD_RE = re.compile(r"^\s*(impact|start|end|components)\s*:\s*(.*?)\s*$", re.IGNORECASE)
CHECKED_RE = re.compile(r"^\s*-\s*\[[xX]\]\s*.*?`([a-z0-9-]+)`")
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
NO_RESPONSE = "_No response_"
TEXT_SECTIONS = ("message", "description", "details", "what is happening", "what will happen", "update")


def log(message: str) -> None:
    print(f"issues: {message}", file=sys.stderr)


# --------------------------------------------------------------------------- body parsing


def parse_time(value: str) -> str | None:
    """ISO 8601 with offset ('2026-11-02T20:00+01:00'), 'Z', or a bare 'YYYY-MM-DD HH:MM' (UTC)."""
    value = value.strip().replace(" UTC", "").replace("Z", "+00:00")
    if " " in value and "T" not in value:
        value = value.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return iso(dt.replace(second=0))


def parse_body(body: str | None) -> tuple[dict, str, set[str]]:
    """Return (fields, text, checked component ids) from an issue body written either through
    the issue form ('### Heading' sections) or by hand ('start: ...' lines plus free text)."""
    body = COMMENT_RE.sub("", (body or "").replace("\r\n", "\n"))
    fields: dict[str, str] = {}
    checked: set[str] = set()
    texts: list[str] = []
    if re.search(r"^### ", body, re.MULTILINE):
        for chunk in re.split(r"^### ", body, flags=re.MULTILINE)[1:]:
            heading, _, content = chunk.partition("\n")
            name = heading.strip().rstrip("?").lower()
            content = content.strip()
            if content == NO_RESPONSE:
                content = ""
            for line in content.splitlines():
                match = CHECKED_RE.match(line)
                if match:
                    checked.add(match.group(1))
            if name in ("impact", "start", "end"):
                fields[name] = content.splitlines()[0].strip() if content else ""
            elif name.startswith(TEXT_SECTIONS):
                texts.append(content)
    else:
        rest = []
        for line in body.split("\n"):
            match = FIELD_RE.match(line)
            if match:
                fields[match.group(1).lower()] = match.group(2)
            else:
                rest.append(line)
        texts.append("\n".join(rest).strip())
    if "components" in fields:
        checked |= {c.strip() for c in fields.pop("components").split(",") if c.strip()}
    return fields, "\n\n".join(t for t in texts if t), checked


def split_keyword(text: str, allowed: list[str], default: str) -> tuple[str, str]:
    match = KEYWORD_RE.match(text)
    if match:
        word = match.group(1).lower().replace(" ", "-")
        if word in allowed:
            rest = text[match.end():].strip()
            return word, rest[:1].upper() + rest[1:]
    return default, text.strip()


# --------------------------------------------------------------------------- mapping


def issue_to_item(issue: dict, authors: set[str], component_ids: set[str]) -> dict | None:
    """One issue (API shape plus a `comments_list`) -> incident/maintenance dict, or None."""
    if issue.get("pull_request"):
        return None
    login = (issue.get("user") or {}).get("login")
    if login not in authors:
        return None
    labels = {(l.get("name") if isinstance(l, dict) else str(l)) for l in issue.get("labels") or []}
    if "incident" in labels:
        kind = "incident"
    elif "maintenance" in labels:
        kind = "maintenance"
    else:
        return None
    number = issue["number"]
    if issue.get("state") == "closed" and issue.get("state_reason") == "not_planned":
        return None  # withdrawn or cancelled

    fields, text, checked = parse_body(issue.get("body"))
    label_components = {l.split(":", 1)[1] for l in labels if l.startswith("component:")}
    components = sorted((label_components | checked) & component_ids)
    unknown = (label_components | checked) - component_ids
    if unknown:
        log(f"#{number}: ignoring unknown components {sorted(unknown)}")

    created = parse_time(issue["created_at"])
    keywords = MAINTENANCE_KEYWORDS if kind == "maintenance" else INCIDENT_KEYWORDS
    first_status, first_body = split_keyword(text, keywords, "scheduled" if kind == "maintenance" else "investigating")
    updates = [{"status": first_status, "at": created, "body": first_body}]
    for comment in issue.get("comments_list") or []:
        if (comment.get("user") or {}).get("login") not in authors:
            continue
        status, body = split_keyword(COMMENT_RE.sub("", comment.get("body") or ""), keywords, "update")
        updates.append({"status": status, "at": parse_time(comment["created_at"]), "body": body})
    updates.sort(key=lambda u: u["at"])

    item = {
        "id": f"issue-{number}",
        "title": issue.get("title") or f"Issue {number}",
        "components": components,
        "auto": False,
        "source": "issue",
        "created_at": created,
        "resolved_at": None,
        "updates": updates,
    }
    final = "completed" if kind == "maintenance" else "resolved"
    if issue.get("state") == "closed" and issue.get("closed_at"):
        closed = parse_time(issue["closed_at"])
        if updates[-1]["status"] != final:
            default = "The maintenance is complete." if kind == "maintenance" else "This incident has been resolved."
            updates.append({"status": final, "at": closed, "body": default})
        item["resolved_at"] = updates[-1]["at"] if updates[-1]["status"] == final else closed
    elif updates[-1]["status"] == final:
        item["resolved_at"] = updates[-1]["at"]

    if kind == "maintenance":
        start, end = parse_time(fields.get("start", "")), parse_time(fields.get("end", ""))
        if not start or not end or end <= start:
            log(f"#{number}: maintenance needs valid 'start' and 'end' times - skipped")
            return None
        item.update(impact="maintenance", scheduled_start=start, scheduled_end=end)
    else:
        impact_labels = [l.split(":", 1)[1] for l in labels if l.startswith("impact:")]
        impact = (impact_labels[0] if impact_labels else fields.get("impact", "")).strip().lower() or "minor"
        item["impact"] = impact if impact in IMPACTS else "minor"
    return item


def items_from_issues(issues: list[dict], authors: set[str], component_ids: set[str]) -> list[dict]:
    items = []
    for issue in issues:
        item = issue_to_item(issue, authors, component_ids)
        if item:
            items.append(item)
    return items


# --------------------------------------------------------------------------- GitHub API


def _get(url: str, token: str) -> list:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "lktec-status",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as resp:  # noqa: S310 (fixed https host)
        return json.load(resp)


def fetch_issues(repo: str, token: str, max_pages: int = 5) -> list[dict]:
    """Every issue labelled `incident` or `maintenance` (open and closed), each with its
    comments attached as `comments_list`. Author filtering happens in issue_to_item."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError(f"invalid repository name {repo!r}")
    seen: dict[int, dict] = {}
    for label in ("incident", "maintenance"):
        for page in range(1, max_pages + 1):
            query = urllib.parse.urlencode({"state": "all", "labels": label, "per_page": 100, "page": page})
            batch = _get(f"https://api.github.com/repos/{repo}/issues?{query}", token)
            for issue in batch:
                seen[issue["number"]] = issue
            if len(batch) < 100:
                break
    for issue in seen.values():
        issue["comments_list"] = []
        if issue.get("comments"):
            query = urllib.parse.urlencode({"per_page": 100})
            issue["comments_list"] = _get(
                f"https://api.github.com/repos/{repo}/issues/{issue['number']}/comments?{query}", token
            )
    keep = ("number", "title", "body", "user", "labels", "state", "state_reason", "created_at", "closed_at",
            "pull_request", "comments_list")
    result = []
    for issue in sorted(seen.values(), key=lambda i: i["number"]):
        slim = {k: issue.get(k) for k in keep}
        slim["user"] = {"login": (issue.get("user") or {}).get("login")}
        slim["labels"] = [{"name": l.get("name")} for l in issue.get("labels") or []]
        slim["comments_list"] = [
            {"user": {"login": (c.get("user") or {}).get("login")}, "body": c.get("body"), "created_at": c.get("created_at")}
            for c in issue["comments_list"]
        ]
        result.append(slim)
    return result
