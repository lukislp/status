"""Static site rendering: index, incident pages, incident history, status.json and an Atom
feed. Plain string templates on purpose - the page is small enough that a template engine
would be the largest dependency in the repository."""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

from .config import all_components
from .manual import maintenance_state, phase
from .model import (
    BANNER_LABELS,
    STATUS_FOR_IMPACT,
    STATUS_LABELS,
    day_uptime,
    format_duration,
    iso,
    parse_iso,
    range_uptime,
    worst,
    worst_impact,
)

BAR_DAYS = 90
UPDATE_LABELS = {
    "investigating": "Investigating",
    "identified": "Identified",
    "monitoring": "Monitoring",
    "update": "Update",
    "resolved": "Resolved",
    "scheduled": "Scheduled",
    "in-progress": "In progress",
    "verifying": "Verifying",
    "completed": "Completed",
}
IMPACT_LABELS = {
    "none": "No impact",
    "minor": "Minor",
    "major": "Major",
    "critical": "Critical",
    "maintenance": "Maintenance",
}
# Bar colour ranks, lowest first.
BAR_RANK = ["none", "ok", "maintenance", "minor", "major", "critical"]


# --------------------------------------------------------------------------- formatting


def fmt_dt(value: str) -> str:
    dt = parse_iso(value)
    return dt.strftime("%b %d, %Y - %H:%M UTC").replace(" 0", " ", 1)


def fmt_time(value: str) -> str:
    return parse_iso(value).strftime("%H:%M UTC")


def fmt_day(day: str) -> str:
    dt = datetime.strptime(day, "%Y-%m-%d")
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}"


def fmt_uptime(value: float | None) -> str:
    if value is None:
        return "No data yet"
    if value >= 100.0:
        return "100.0 % uptime"
    # Truncate rather than round so 99.996 never reads as a perfect 100.00.
    return f"{int(value * 100) / 100:.2f} % uptime"


_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+|[a-zA-Z0-9_./#-]+)\)")


def inline_md(text: str) -> str:
    """Escape, then allow **bold**, `code` and [links](https://...)."""
    out = escape(text, quote=True)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    out = _LINK_RE.sub(r'<a href="\2">\1</a>', out)
    return out


def md_block(text: str) -> str:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return "".join(f"<p>{inline_md(' '.join(p.splitlines()))}</p>" for p in paragraphs)


# --------------------------------------------------------------------------- model helpers


def incident_start(incident: dict) -> str:
    """When an incident began affecting things. Maintenance counts from its window (or from
    an earlier in-progress update), not from the day it was announced."""
    if incident["impact"] == "maintenance" and incident.get("scheduled_start"):
        started = [u["at"] for u in incident["updates"] if u["status"] != "scheduled"]
        return min([incident["scheduled_start"], *started])
    return incident["created_at"]


def incident_days(incident: dict, now_iso: str) -> tuple[str, str]:
    start = incident_start(incident)
    if incident.get("resolved_at"):
        end = incident["resolved_at"]
    elif phase(incident, now_iso) == "active":
        end = now_iso
    else:
        end = incident.get("scheduled_end") or now_iso
    return start[:10], max(start, end)[:10]


def bar_class_for_uptime(uptime: float | None) -> str:
    if uptime is None:
        return "none"
    if uptime >= 99.9:
        return "ok"
    if uptime >= 99.0:
        return "minor"
    if uptime >= 95.0:
        return "major"
    return "critical"


def effective_status(comp: dict, state: dict | None, active: list[dict]) -> str:
    if comp.get("planned"):
        return "planned"
    statuses = []
    if state and state.get("status") not in (None, "planned"):
        statuses.append(state["status"])
    for incident in active:
        if comp["id"] in incident["components"] and not incident.get("auto"):
            statuses.append(STATUS_FOR_IMPACT[incident["impact"]])
    if not statuses:
        return "unknown"
    return worst(statuses)


class Context:
    def __init__(self, config: dict, data: dict, manual: list[dict], now: datetime):
        self.config = config
        self.site = config["site"]
        self.settings = config["settings"]
        self.data = data
        self.now = now
        self.now_iso = iso(now)
        self.today = now.strftime("%Y-%m-%d")
        self.incidents = sorted(
            manual + data["incidents"]["incidents"], key=lambda i: i["updates"][-1]["at"], reverse=True
        )
        self.active = [i for i in self.incidents if phase(i, self.now_iso) == "active"]
        self.upcoming = [i for i in self.incidents if phase(i, self.now_iso) == "upcoming"]
        self.days = [(now - timedelta(days=BAR_DAYS - 1 - n)).strftime("%Y-%m-%d") for n in range(BAR_DAYS)]
        self.components = all_components(config)
        self.names = {c["id"]: c["name"] for c in self.components}

    def state(self, cid: str) -> dict | None:
        return self.data["status"]["components"].get(cid)

    def status_of(self, comp: dict) -> str:
        return effective_status(comp, self.state(comp["id"]), self.active)

    def overall(self) -> str:
        statuses = [self.status_of(c) for c in self.components]
        for incident in self.active:
            if not incident["components"] and not incident.get("auto"):
                statuses.append(STATUS_FOR_IMPACT[incident["impact"]])
        return worst(s for s in statuses if s not in ("planned", "unknown"))

    def uptime(self, cid: str) -> float | None:
        return range_uptime(self.data["history"]["days"].get(cid, {}), self.days)


# --------------------------------------------------------------------------- page chrome


def page(ctx: Context, title: str, body: str, prefix: str = "", description: str = "") -> str:
    site = ctx.site
    full_title = f"{title} - {site['title']}" if title else site["title"]
    description = description or f"Live status, uptime and incident history for {site.get('home_url', '').removeprefix('https://')} and its demos."
    legal = []
    if site.get("imprint_url"):
        legal.append(f'<a href="{escape(site["imprint_url"])}">Imprint</a>')
    if site.get("privacy_url"):
        legal.append(f'<a href="{escape(site["privacy_url"])}">Privacy</a>')
    links = [
        f'<a href="{escape(site["home_url"])}">{escape(site["home_url"].removeprefix("https://"))}</a>' if site.get("home_url") else "",
        f'<a href="{prefix}history.html">Incident history</a>',
        f'<a href="{escape(site["repo_url"])}">Source</a>' if site.get("repo_url") else "",
        *legal,
    ]
    privacy_link = (
        f' See the <a href="{escape(site["privacy_url"])}">privacy policy</a>.' if site.get("privacy_url") else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'">
<meta name="referrer" content="strict-origin-when-cross-origin">
<meta name="color-scheme" content="light dark">
<meta name="description" content="{escape(description)}">
<title>{escape(full_title)}</title>
<link rel="icon" href="{prefix}favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="{prefix}style.css">
<link rel="alternate" type="application/atom+xml" title="{escape(site['title'])} incidents" href="{prefix}feed.xml">
<script src="{prefix}bars.js" defer></script>
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<header class="masthead">
  <div class="wrap masthead-inner">
    <a class="brand" href="{prefix or './'}" aria-label="{escape(site['title'])} home">
      <svg class="brand-mark" viewBox="0 0 32 32" aria-hidden="true" focusable="false"><rect width="32" height="32" rx="8"/><path d="M6 17h5l3-7 4 13 3-6h5" fill="none" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
      <span class="brand-name">{escape(site['title'].split(' ')[0])}</span><span class="brand-sub">{escape(' '.join(site['title'].split(' ')[1:]))}</span>
    </a>
    <a class="home-link" href="{escape(site.get('home_url') or './')}">{escape((site.get('home_url') or '').removeprefix('https://'))} <span aria-hidden="true">&rarr;</span></a>
  </div>
</header>
<main id="main" class="wrap">
{body}
</main>
<footer class="wrap footer">
  <nav aria-label="Footer">{' '.join(l for l in links if l)}</nav>
  <p class="privacy">Hosted on GitHub Pages, which logs visitor IP addresses for security purposes. This page loads nothing from third parties, sets no cookies and uses no analytics.{privacy_link}</p>
  <p class="owner">&copy; {ctx.now.year} {escape(site.get('owner_name', ''))}</p>
</footer>
</body>
</html>
"""


# --------------------------------------------------------------------------- index


def render_incident_card(ctx: Context, incident: dict, prefix: str) -> str:
    impact = incident["impact"]
    rows = []
    for update in reversed(incident["updates"]):
        rows.append(
            f'<div class="update"><strong>{UPDATE_LABELS.get(update["status"], update["status"].title())}</strong>'
            f' - <span class="update-body">{inline_md(" ".join(update["body"].split()))}</span>'
            f'<time datetime="{update["at"]}">{fmt_dt(update["at"])}</time></div>'
        )
    affected = ", ".join(ctx.names.get(c, c) for c in incident["components"])
    when = ""
    if impact == "maintenance" and incident.get("scheduled_start"):
        when = f'<p class="window">Scheduled for {fmt_dt(incident["scheduled_start"])} to {fmt_dt(incident["scheduled_end"])}</p>'
    return f"""<section class="incident-card impact-{impact}" aria-labelledby="ic-{escape(incident['id'])}">
  <h2 class="incident-card-title" id="ic-{escape(incident['id'])}"><a href="{prefix}incidents/{escape(incident['id'])}.html">{escape(incident['title'])}</a></h2>
  <div class="incident-card-body">{when}{''.join(rows)}{f'<p class="affects">This affects: {escape(affected)}.</p>' if affected else ''}</div>
</section>"""


def render_bars(ctx: Context, comp: dict) -> str:
    cid = comp["id"]
    days = ctx.data["history"]["days"].get(cid, {})
    per_day: dict[str, list[dict]] = {d: [] for d in ctx.days}
    for incident in ctx.incidents:
        if cid not in incident["components"] or phase(incident, ctx.now_iso) == "upcoming":
            continue
        start, end = incident_days(incident, ctx.now_iso)
        for d in ctx.days:
            if start <= d <= end:
                per_day[d].append(incident)
    items = []
    for index, day in enumerate(ctx.days):
        entry = days.get(day)
        uptime = None if comp.get("planned") else day_uptime(entry)
        cls = bar_class_for_uptime(uptime)
        impacts = [i["impact"] for i in per_day[day]]
        if impacts:
            inc_cls = {"none": "ok", "maintenance": "maintenance"}.get(worst_impact(impacts), worst_impact(impacts))
            cls = max(cls, inc_cls, key=BAR_RANK.index)
        if uptime is None:
            summary = "No data" if not comp.get("planned") else "Not live yet"
        else:
            summary = fmt_uptime(uptime)
            if entry.get("lat_n"):
                summary += f" - avg {round(entry['lat_sum'] / entry['lat_n'])} ms"
        if per_day[day]:
            inc_html = "".join(
                f'<li class="tip-incident impact-{escape(i["impact"])}">{escape(i["title"])}</li>' for i in per_day[day]
            )
            inc_text = "; ".join(i["title"] for i in per_day[day])
            tip_incidents = f'<ul class="tip-incidents">{inc_html}</ul>'
        else:
            inc_text = "no incidents"
            tip_incidents = '<span class="tip-quiet">No incidents</span>'
        side = " tip-left" if index < 12 else (" tip-right" if index > BAR_DAYS - 13 else "")
        label = f"{fmt_day(day)}: {summary}, {inc_text}"
        tabindex = "0" if index == BAR_DAYS - 1 else "-1"
        items.append(
            f'<li class="bar b-{cls}{side}" role="img" tabindex="{tabindex}" aria-label="{escape(label)}">'
            f'<span class="tip" aria-hidden="true"><strong>{fmt_day(day)}</strong><span>{escape(summary)}</span>{tip_incidents}</span></li>'
        )
    return "".join(items)


def render_component(ctx: Context, comp: dict) -> str:
    status = ctx.status_of(comp)
    uptime = None if comp.get("planned") else ctx.uptime(comp["id"])
    uptime_text = "Not live yet" if comp.get("planned") else fmt_uptime(uptime)
    help_html = ""
    if comp.get("description"):
        help_html = (
            f'<span class="help" tabindex="0" role="note" aria-label="{escape(comp["description"])}">?'
            f'<span class="help-tip" aria-hidden="true">{escape(comp["description"])}</span></span>'
        )
    return f"""<div class="component" data-status="{status}">
  <div class="component-head">
    <h3 class="component-name">{escape(comp['name'])}</h3>{help_html}
    <span class="component-status s-{status}">{STATUS_LABELS[status]}</span>
  </div>
  <ol class="bars" aria-label="Daily uptime of {escape(comp['name'])} over the past {BAR_DAYS} days, oldest first. Use the arrow keys to move between days.">{render_bars(ctx, comp)}</ol>
  <div class="bars-legend" aria-hidden="true">
    <span class="span-90">90 days ago</span><span class="span-60">60 days ago</span><span class="span-30">30 days ago</span>
    <span class="rule"></span><span class="uptime">{uptime_text}</span><span class="rule"></span><span>Today</span>
  </div>
</div>"""


MAINT_STATE_LABELS = {
    "scheduled": "Scheduled",
    "in-progress": "In progress",
    "verifying": "Verifying",
    "completed": "Completed",
}


def fmt_window(start: str, end: str) -> str:
    a, b = parse_iso(start), parse_iso(end)
    minutes = int((b - a).total_seconds() // 60)
    if a.date() == b.date():
        span = f"{a.strftime('%a, %b')} {a.day} &middot; {a.strftime('%H:%M')}&ndash;{b.strftime('%H:%M')} UTC"
    else:
        span = (
            f"{a.strftime('%a, %b')} {a.day}, {a.strftime('%H:%M')} &ndash; "
            f"{b.strftime('%a, %b')} {b.day}, {b.strftime('%H:%M')} UTC"
        )
    return f'{span} <span class="mw-duration">({format_duration(minutes)})</span>'


def shown_maintenance(ctx: Context) -> list[dict]:
    """Upcoming and running windows, plus windows that ended within the last 24 hours."""
    recent = iso(ctx.now - timedelta(hours=24))
    items = []
    for item in ctx.incidents:
        if item["impact"] != "maintenance":
            continue
        state = maintenance_state(item, ctx.now_iso)
        ended = item.get("resolved_at") or item["scheduled_end"]
        if state != "completed" or ended >= recent:
            items.append(item)
    order = {"in-progress": 0, "verifying": 0, "scheduled": 1, "completed": 2}
    return sorted(items, key=lambda i: (order[maintenance_state(i, ctx.now_iso)], i["scheduled_start"]))


def render_maintenance(ctx: Context, prefix: str = "") -> str:
    items = shown_maintenance(ctx)
    if not items:
        return ""
    rows = []
    for item in items:
        state = maintenance_state(item, ctx.now_iso)
        start = parse_iso(item["scheduled_start"])
        latest = item["updates"][-1]
        tags = "".join(f"<li>{escape(ctx.names.get(c, c))}</li>" for c in item["components"])
        text = f'<p class="mw-text">{inline_md(" ".join(latest["body"].split()))}</p>' if latest["body"] else ""
        tag_list = f'<ul class="mw-tags" aria-label="Affected components">{tags}</ul>' if tags else ""
        rows.append(
            f"""<li class="mw mw-{state}">
  <div class="mw-date" aria-hidden="true"><span class="mw-month">{start.strftime('%b')}</span><span class="mw-day">{start.day}</span></div>
  <div class="mw-main">
    <div class="mw-head"><h3 class="mw-title"><a href="{prefix}incidents/{escape(item['id'])}.html">{escape(item['title'])}</a></h3>
    <span class="mw-state"><span class="mw-dot" aria-hidden="true"></span>{MAINT_STATE_LABELS[state]}</span></div>
    <p class="mw-window">{fmt_window(item['scheduled_start'], item['scheduled_end'])}</p>
    {text}{tag_list}
  </div>
</li>"""
        )
    return f"""<section class="maintenance" aria-labelledby="maintenance-title">
  <h2 class="maintenance-title" id="maintenance-title"><svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M10.5 1.5a4 4 0 0 0-3.8 5.2L1.9 11.5a1.4 1.4 0 0 0 2 2l4.8-4.8a4 4 0 0 0 5.2-3.8l-2.2 2.2-2-.6-.6-2z"/></svg>Planned maintenance</h2>
  <ol class="mw-list">{''.join(rows)}</ol>
</section>"""


def render_index(ctx: Context) -> str:
    overall = ctx.overall()
    cards = "".join(render_incident_card(ctx, i, "") for i in ctx.active if i["impact"] != "maintenance")
    groups = []
    for group in ctx.config["groups"]:
        comps = "".join(render_component(ctx, c) for c in group["components"])
        live = [ctx.status_of(c) for c in group["components"]]
        group_status = worst(s for s in live if s not in ("planned", "unknown"))
        groups.append(
            f'<section class="group" aria-labelledby="g-{escape(group["name"].lower())}">'
            f'<h2 class="group-name" id="g-{escape(group["name"].lower())}"><span>{escape(group["name"])}</span>'
            f'<span class="group-status s-{group_status}">{STATUS_LABELS[group_status]}</span></h2>{comps}</section>'
        )
    checked = ctx.data["status"].get("generated_at")
    checked_html = f'<p class="checked">Last checked <time datetime="{checked}">{fmt_dt(checked)}</time></p>' if checked else ""
    body = f"""{cards}
{render_maintenance(ctx)}
<div class="banner banner-{overall}" role="status"><span class="banner-icon" aria-hidden="true"></span><span>{BANNER_LABELS[overall]}</span></div>
<div class="components">
  <p class="components-intro">Uptime over the past {BAR_DAYS} days.</p>
  {''.join(groups)}
</div>
{checked_html}
<p class="history-row"><a class="history-button" href="history.html">Incident history</a></p>"""
    return page(ctx, "", body)


# --------------------------------------------------------------------------- incident pages


def render_incident_page(ctx: Context, incident: dict) -> str:
    prefix = "../"
    rows = []
    for update in reversed(incident["updates"]):
        rows.append(
            f'<div class="update-row"><div class="update-status">{UPDATE_LABELS.get(update["status"], update["status"].title())}</div>'
            f'<div class="update-text">{md_block(update["body"]) or "<p></p>"}<p class="meta">Posted <time datetime="{update["at"]}">{fmt_dt(update["at"])}</time></p></div></div>'
        )
    affected = ", ".join(ctx.names.get(c, c) for c in incident["components"])
    window = ""
    if incident["impact"] == "maintenance" and incident.get("scheduled_start"):
        window = f'<p class="window">Scheduled for {fmt_dt(incident["scheduled_start"])} to {fmt_dt(incident["scheduled_end"])}</p>'
    origin = (
        '<p class="meta">Opened automatically by the uptime checks.</p>' if incident.get("auto") else ""
    )
    body = f"""<p class="crumb"><a href="{prefix}">&larr; Current status</a></p>
<h1 class="incident-title impact-text-{incident['impact']}">{escape(incident['title'])}</h1>
<p class="impact-pill impact-{incident['impact']}">{IMPACT_LABELS[incident['impact']]}</p>
{window}{origin}
<div class="updates">{''.join(rows)}</div>
{f'<p class="affects">This incident affected: {escape(affected)}.</p>' if affected else ''}"""
    return page(ctx, incident["title"], body, prefix, description=f"Incident report: {incident['title']}")


def render_history(ctx: Context) -> str:
    months: dict[str, list[dict]] = {}
    for incident in ctx.incidents:
        if phase(incident, ctx.now_iso) == "upcoming":
            continue
        months.setdefault(incident_start(incident)[:7], []).append(incident)
    sections = []
    for month in sorted(months, reverse=True):
        label = datetime.strptime(month, "%Y-%m").strftime("%B %Y")
        items = []
        for incident in sorted(months[month], key=incident_start, reverse=True):
            start = incident_start(incident)
            end = incident.get("resolved_at")
            span = fmt_dt(start) + (f" to {fmt_time(end) if end[:10] == start[:10] else fmt_dt(end)}" if end else " - ongoing")
            body = " ".join(incident["updates"][-1]["body"].split())
            items.append(
                f'<li class="history-item"><h3 class="impact-text-{incident["impact"]}"><a href="incidents/{escape(incident["id"])}.html">{escape(incident["title"])}</a></h3>'
                f'<p>{inline_md(body)}</p><p class="meta">{span}</p></li>'
            )
        sections.append(f'<section class="month"><h2>{label}</h2><ul class="history-list">{"".join(items)}</ul></section>')
    if not sections:
        sections.append('<p class="quiet">No incidents recorded yet.</p>')
    body = f"""<p class="crumb"><a href="./">&larr; Current status</a></p>
<h1 class="page-title">Incident history</h1>
{''.join(sections)}"""
    return page(ctx, "Incident history", body)


def render_404(ctx: Context) -> str:
    body = """<h1 class="page-title">Page not found</h1>
<p>That page does not exist. <a href="/">Back to the current status</a>.</p>"""
    return page(ctx, "Not found", body, "/")


# --------------------------------------------------------------------------- machine readable


def render_status_json(ctx: Context) -> str:
    overall = ctx.overall()
    components = []
    for group in ctx.config["groups"]:
        for comp in group["components"]:
            state = ctx.state(comp["id"]) or {}
            uptime = None if comp.get("planned") else ctx.uptime(comp["id"])
            components.append(
                {
                    "id": comp["id"],
                    "name": comp["name"],
                    "group": group["name"],
                    "status": ctx.status_of(comp),
                    "uptime_90d": round(uptime, 3) if uptime is not None else None,
                    "latency_ms": state.get("latency_ms"),
                    "checked_at": state.get("checked_at"),
                }
            )
    doc = {
        "page": {"name": ctx.site["title"], "url": ctx.site["base_url"], "updated_at": ctx.data["status"].get("generated_at")},
        "status": {"indicator": overall, "description": BANNER_LABELS[overall]},
        "components": components,
        "maintenance": [
            {
                "id": i["id"],
                "title": i["title"],
                "state": maintenance_state(i, ctx.now_iso),
                "start": i["scheduled_start"],
                "end": i["scheduled_end"],
                "components": i["components"],
            }
            for i in shown_maintenance(ctx)
        ],
        "incidents": [
            {
                "id": i["id"],
                "title": i["title"],
                "impact": i["impact"],
                "status": i["updates"][-1]["status"],
                "components": i["components"],
                "url": f"{ctx.site['base_url']}/incidents/{i['id']}.html",
            }
            for i in ctx.active
            if i["impact"] != "maintenance"
        ],
    }
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def render_feed(ctx: Context) -> str:
    base = ctx.site["base_url"].rstrip("/")
    entries = []
    for incident in ctx.incidents[:50]:
        url = f"{base}/incidents/{incident['id']}.html"
        content = "".join(
            f"<p><strong>{UPDATE_LABELS.get(u['status'], u['status'])}</strong> ({fmt_dt(u['at'])}) - {inline_md(' '.join(u['body'].split()))}</p>"
            for u in reversed(incident["updates"])
        )
        entries.append(
            f"""  <entry>
    <id>{escape(url)}</id>
    <title>{escape(incident['title'])}</title>
    <link rel="alternate" type="text/html" href="{escape(url)}"/>
    <published>{incident['updates'][0]['at']}</published>
    <updated>{incident['updates'][-1]['at']}</updated>
    <content type="html">{escape(content)}</content>
  </entry>"""
        )
    updated = ctx.incidents[0]["updates"][-1]["at"] if ctx.incidents else (ctx.data["status"].get("generated_at") or ctx.now_iso)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <id>{escape(base)}/</id>
  <title>{escape(ctx.site['title'])} - incidents</title>
  <link rel="alternate" type="text/html" href="{escape(base)}/"/>
  <link rel="self" type="application/atom+xml" href="{escape(base)}/feed.xml"/>
  <updated>{updated}</updated>
  <author><name>{escape(ctx.site.get('owner_name') or ctx.site['title'])}</name></author>
{chr(10).join(entries)}
</feed>
"""


# --------------------------------------------------------------------------- build


def build_site(config: dict, data: dict, manual: list[dict], now: datetime, assets: Path, out: Path) -> list[Path]:
    ctx = Context(config, data, manual, now)
    if out.exists():
        shutil.rmtree(out)
    (out / "incidents").mkdir(parents=True)
    written = []

    def write(rel: str, text: str) -> None:
        path = out / rel
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)

    write("index.html", render_index(ctx))
    write("history.html", render_history(ctx))
    write("404.html", render_404(ctx))
    for incident in ctx.incidents:
        write(f"incidents/{incident['id']}.html", render_incident_page(ctx, incident))
    write("status.json", render_status_json(ctx))
    write("feed.xml", render_feed(ctx))
    host = ctx.site["base_url"].split("://", 1)[-1].strip("/")
    write("CNAME", host + "\n")
    write(".nojekyll", "")
    for asset in sorted(assets.iterdir()):
        if asset.is_file():
            shutil.copyfile(asset, out / asset.name)
            written.append(out / asset.name)
    return written
