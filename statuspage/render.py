"""Static site rendering: index, incident pages, incident history, status.json and an Atom
feed. Plain string templates on purpose - the page is small enough that a template engine
would be the largest dependency in the repository."""

from __future__ import annotations

import json
import posixpath
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

from . import i18n
from .config import all_components, localized
from .i18n import DEFAULT_LANG, LANGS, OTHER, t
from .manual import maintenance_state, phase
from .model import (
    STATUS_FOR_IMPACT,
    day_uptime,
    iso,
    parse_iso,
    range_uptime,
    worst,
    worst_impact,
)

BAR_DAYS = 90
SUMMARY_DAYS = 30  # window of `uptime_30d` in status.json
# Bar colour ranks, lowest first.
BAR_RANK = ["none", "ok", "maintenance", "minor", "major", "critical"]


# --------------------------------------------------------------------------- formatting


def fmt_dt(value: str, lang: str) -> str:
    return i18n.fmt_dt(parse_iso(value), lang)


def fmt_time(value: str) -> str:
    return parse_iso(value).strftime("%H:%M UTC")


def fmt_day(day: str, lang: str) -> str:
    return i18n.fmt_day(datetime.strptime(day, "%Y-%m-%d"), lang)


def fmt_uptime(value: float | None, lang: str) -> str:
    if value is None:
        return t(lang, "uptime.none")
    if value >= 100.0:
        return t(lang, "uptime.pct", value=i18n.fmt_number(100.0, 1, lang))
    # Truncate rather than round so 99.996 never reads as a perfect 100.00.
    return t(lang, "uptime.pct", value=i18n.fmt_number(int(value * 100) / 100, 2, lang))


# --------------------------------------------------------------------------- paths


def lang_path(lang: str, rel: str) -> str:
    """Where a page lives in the built site: German at the root, English under en/."""
    return rel if lang == DEFAULT_LANG else f"en/{rel}"


def rel_link(source: str, target: str) -> str:
    """A relative link from the page `source` to `target` (both site paths); an index.html
    target is linked as its directory."""
    link = posixpath.relpath(target, posixpath.dirname(source) or ".")
    if posixpath.basename(target) == "index.html":
        link = link[: -len("index.html")] or "./"
    return link


@dataclass(frozen=True)
class Loc:
    """One page: its language and its language-neutral path (index.html, history.html,
    incidents/<id>.html). Pages link relatively, so the site works from any directory; the
    404 page is served for any path and links absolutely."""

    lang: str
    rel: str
    absolute: bool = False

    @property
    def path(self) -> str:
        return lang_path(self.lang, self.rel)

    def link(self, rel: str, lang: str | None = None) -> str:
        target = lang_path(lang or self.lang, rel)
        if self.absolute:
            return "/" + (target[: -len("index.html")] if target.endswith("index.html") else target)
        return rel_link(self.path, target)

    def asset(self, name: str) -> str:
        return f"/{name}" if self.absolute else rel_link(self.path, name)


def abs_url(ctx, lang: str, rel: str) -> str:
    target = lang_path(lang, rel)
    if target.endswith("index.html"):
        target = target[: -len("index.html")]
    return f"{ctx.site['base_url'].rstrip('/')}/{target}"


def site_url(ctx, key: str, lang: str) -> str:
    """imprint_url / privacy_url, in German when `<key>_de` is set."""
    site = ctx.site
    return (site.get(f"{key}_de") if lang == "de" else None) or site.get(key) or ""


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
        self.by_id = {c["id"]: c for c in self.components}

    def name(self, cid: str, lang: str) -> str:
        comp = self.by_id.get(cid)
        return localized(comp, "name", lang) if comp else cid

    def title(self, incident: dict, lang: str) -> str:
        """An incident's title; automatic incidents are worded per language from their template."""
        if incident.get("tpl"):
            return i18n.render_tpl(lang, incident["tpl"], lambda cid: self.name(cid, lang))
        return incident["title"]

    def body(self, update: dict, lang: str) -> str:
        if update.get("tpl"):
            return i18n.render_tpl(lang, update["tpl"], lambda cid: self.name(cid, lang))
        return update["body"]

    def update_label(self, update: dict, lang: str) -> str:
        key = f"update.{update['status']}"
        return t(lang, key) if key in i18n.STRINGS[lang] else update["status"].title()

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

    def recent_uptime(self, cid: str) -> float | None:
        """Uptime over the last SUMMARY_DAYS days (the tail of the bar days)."""
        return range_uptime(self.data["history"]["days"].get(cid, {}), self.days[-SUMMARY_DAYS:])


# --------------------------------------------------------------------------- page chrome


def lang_switch(loc: Loc, alternates: bool) -> str:
    if not alternates:
        return ""
    items = []
    for lang in LANGS:
        label = lang.upper()
        if lang == loc.lang:
            items.append(f'<span class="lang-current" lang="{lang}" aria-current="true">{label}</span>')
        else:
            items.append(
                f'<a class="lang-link" lang="{lang}" hreflang="{lang}" href="{loc.link(loc.rel, lang)}" '
                f'title="{escape(t(lang, "lang." + lang))}">{label}</a>'
            )
    separator = '<span class="lang-sep" aria-hidden="true">|</span>'
    return f'<nav class="lang-switch" aria-label="{escape(t(loc.lang, "lang.nav"))}">{separator.join(items)}</nav>'


def page(ctx: Context, loc: Loc, title: str, body: str, description: str = "", alternates: bool = True) -> str:
    lang, site = loc.lang, ctx.site
    host = site.get("home_url", "").removeprefix("https://")
    full_title = f"{title} - {site['title']}" if title else site["title"]
    description = description or t(lang, "desc.index", host=host)
    imprint, privacy = site_url(ctx, "imprint_url", lang), site_url(ctx, "privacy_url", lang)
    legal = []
    if imprint:
        legal.append(f'<a href="{escape(imprint)}">{t(lang, "footer.imprint")}</a>')
    if privacy:
        legal.append(f'<a href="{escape(privacy)}">{t(lang, "footer.privacy")}</a>')
    links = [
        f'<a href="{escape(site["home_url"])}">{escape(host)}</a>' if site.get("home_url") else "",
        f'<a href="{loc.link("history.html")}">{t(lang, "footer.history")}</a>',
        f'<a href="{escape(site["repo_url"])}">{t(lang, "footer.source")}</a>' if site.get("repo_url") else "",
        *legal,
    ]
    privacy_anchor = f'<a href="{escape(privacy)}">{t(lang, "footer.privacy_link")}</a>'
    privacy_link = " " + t(lang, "footer.privacy_see", link=privacy_anchor) if privacy else ""
    head_links = []
    if alternates:
        head_links.append(f'<link rel="canonical" href="{escape(abs_url(ctx, lang, loc.rel))}">')
        for other in LANGS:
            head_links.append(f'<link rel="alternate" hreflang="{other}" href="{escape(abs_url(ctx, other, loc.rel))}">')
        head_links.append(f'<link rel="alternate" hreflang="x-default" href="{escape(abs_url(ctx, DEFAULT_LANG, loc.rel))}">')
    head_extra = "\n".join(head_links)
    feed_title = escape(t(lang, "feed.alt", title=site["title"]))
    brand_label = escape(t(lang, "brand.home", title=site["title"]))
    return f"""<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'">
<meta name="referrer" content="strict-origin-when-cross-origin">
<meta name="color-scheme" content="light dark">
<meta name="description" content="{escape(description)}">
<title>{escape(full_title)}</title>
<link rel="icon" href="{loc.asset("favicon.svg")}" type="image/svg+xml">
<link rel="stylesheet" href="{loc.asset("style.css")}">
<link rel="alternate" type="application/atom+xml" title="{feed_title}" href="{loc.link("feed.xml")}">
{head_extra}
<script src="{loc.asset("bars.js")}" defer></script>
</head>
<body>
<a class="skip" href="#main">{t(lang, "skip")}</a>
<header class="masthead">
  <div class="wrap masthead-inner">
    <a class="brand" href="{loc.link("index.html")}" aria-label="{brand_label}">
      <svg class="brand-mark" viewBox="0 0 32 32" aria-hidden="true" focusable="false"><rect width="32" height="32" rx="8"/><path d="M6 17h5l3-7 4 13 3-6h5" fill="none" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
      <span class="brand-name">{escape(site['title'].split(' ')[0])}</span><span class="brand-sub">{escape(' '.join(site['title'].split(' ')[1:]))}</span>
    </a>
    <div class="masthead-end">
      {lang_switch(loc, alternates)}
      <a class="home-link" href="{escape(site.get('home_url') or './')}">{escape(host)} <span aria-hidden="true">&rarr;</span></a>
    </div>
  </div>
</header>
<main id="main" class="wrap">
{body}
</main>
<footer class="wrap footer">
  <nav aria-label="{t(lang, "footer.nav")}">{' '.join(l for l in links if l)}</nav>
  <p class="privacy">{t(lang, "footer.note")}{privacy_link}</p>
  <p class="owner">&copy; {ctx.now.year} {escape(site.get('owner_name', ''))}</p>
</footer>
</body>
</html>
"""


# --------------------------------------------------------------------------- index


FEED_ICON = (
    '<svg class="feed-icon" viewBox="0 0 16 16" aria-hidden="true" focusable="false">'
    '<circle cx="3" cy="13" r="1.8"/><path d="M2 7.5a6.5 6.5 0 0 1 6.5 6.5M2 2.5A11.5 11.5 0 0 1 13.5 14" '
    'fill="none" stroke-width="2" stroke-linecap="round"/></svg>'
)


def feed_link(loc: Loc) -> str:
    return (
        f'<a class="feed-link" href="{loc.link("feed.xml")}" type="application/atom+xml">'
        f'{FEED_ICON}<span>{t(loc.lang, "feed.subscribe")}</span></a>'
    )


def affected_names(ctx: Context, incident: dict, lang: str) -> str:
    return ", ".join(ctx.name(c, lang) for c in incident["components"])


def maintenance_window(incident: dict, lang: str) -> str:
    return t(lang, "maintenance.window", start=fmt_dt(incident["scheduled_start"], lang), end=fmt_dt(incident["scheduled_end"], lang))


def incident_href(loc: Loc, incident: dict) -> str:
    return loc.link("incidents/" + incident["id"] + ".html")


def render_incident_card(ctx: Context, incident: dict, loc: Loc) -> str:
    lang, impact = loc.lang, incident["impact"]
    rows = []
    for update in reversed(incident["updates"]):
        rows.append(
            f'<div class="update"><strong>{escape(ctx.update_label(update, lang))}</strong>'
            f' - <span class="update-body">{inline_md(" ".join(ctx.body(update, lang).split()))}</span>'
            f'<time datetime="{update["at"]}">{fmt_dt(update["at"], lang)}</time></div>'
        )
    affected = affected_names(ctx, incident, lang)
    when = ""
    if impact == "maintenance" and incident.get("scheduled_start"):
        when = f'<p class="window">{maintenance_window(incident, lang)}</p>'
    affects = f'<p class="affects">{escape(t(lang, "affects.card", names=affected))}</p>' if affected else ""
    return f"""<section class="incident-card impact-{impact}" aria-labelledby="ic-{escape(incident['id'])}">
  <h2 class="incident-card-title" id="ic-{escape(incident['id'])}"><a href="{incident_href(loc, incident)}">{escape(ctx.title(incident, lang))}</a></h2>
  <div class="incident-card-body">{when}{''.join(rows)}{affects}</div>
</section>"""


def render_bars(ctx: Context, comp: dict, lang: str) -> str:
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
            summary = t(lang, "bar.no_data" if not comp.get("planned") else "uptime.not_live")
        else:
            summary = fmt_uptime(uptime, lang)
            if entry.get("lat_n"):
                summary += " - " + t(lang, "bar.avg", ms=round(entry["lat_sum"] / entry["lat_n"]))
        if per_day[day]:
            titles = [ctx.title(i, lang) for i in per_day[day]]
            inc_html = "".join(
                f'<li class="tip-incident impact-{escape(i["impact"])}">{escape(title)}</li>'
                for i, title in zip(per_day[day], titles)
            )
            inc_text = "; ".join(titles)
            tip_incidents = f'<ul class="tip-incidents">{inc_html}</ul>'
        else:
            inc_text = t(lang, "bar.no_incidents_text")
            tip_incidents = f'<span class="tip-quiet">{t(lang, "bar.no_incidents")}</span>'
        side = " tip-left" if index < 12 else (" tip-right" if index > BAR_DAYS - 13 else "")
        label = f"{fmt_day(day, lang)}: {summary}, {inc_text}"
        tabindex = "0" if index == BAR_DAYS - 1 else "-1"
        items.append(
            f'<li class="bar b-{cls}{side}" role="img" tabindex="{tabindex}" aria-label="{escape(label)}">'
            f'<span class="tip" aria-hidden="true"><strong>{fmt_day(day, lang)}</strong><span>{escape(summary)}</span>{tip_incidents}</span></li>'
        )
    return "".join(items)


def render_component(ctx: Context, comp: dict, lang: str) -> str:
    status = ctx.status_of(comp)
    uptime = None if comp.get("planned") else ctx.uptime(comp["id"])
    uptime_text = t(lang, "uptime.not_live") if comp.get("planned") else fmt_uptime(uptime, lang)
    name, description = localized(comp, "name", lang), localized(comp, "description", lang)
    help_html = ""
    if description:
        help_html = (
            f'<span class="help" tabindex="0" role="note" aria-label="{escape(description)}">?'
            f'<span class="help-tip" aria-hidden="true">{escape(description)}</span></span>'
        )
    bars_label = escape(t(lang, "component.bars", name=name, days=BAR_DAYS))
    return f"""<div class="component" data-status="{status}">
  <div class="component-head">
    <h3 class="component-name">{escape(name)}</h3>{help_html}
    <span class="component-status s-{status}">{t(lang, "status." + status)}</span>
  </div>
  <ol class="bars" aria-label="{bars_label}">{render_bars(ctx, comp, lang)}</ol>
  <div class="bars-legend" aria-hidden="true">
    <span class="span-90">{t(lang, "legend.90")}</span><span class="span-60">{t(lang, "legend.60")}</span><span class="span-30">{t(lang, "legend.30")}</span>
    <span class="rule"></span><span class="uptime">{uptime_text}</span><span class="rule"></span><span>{t(lang, "legend.today")}</span>
  </div>
</div>"""


def fmt_window(start: str, end: str, lang: str) -> str:
    a, b = parse_iso(start), parse_iso(end)
    minutes = int((b - a).total_seconds() // 60)
    if a.date() == b.date():
        span = f"{i18n.fmt_short_date(a, lang)} &middot; {a.strftime('%H:%M')}&ndash;{b.strftime('%H:%M')} UTC"
    else:
        span = (
            f"{i18n.fmt_short_date(a, lang)}, {a.strftime('%H:%M')} &ndash; "
            f"{i18n.fmt_short_date(b, lang)}, {b.strftime('%H:%M')} UTC"
        )
    return f'{span} <span class="mw-duration">({i18n.fmt_duration(minutes, lang)})</span>'


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


def render_maintenance(ctx: Context, loc: Loc) -> str:
    lang = loc.lang
    items = shown_maintenance(ctx)
    if not items:
        return ""
    rows = []
    for item in items:
        state = maintenance_state(item, ctx.now_iso)
        start = parse_iso(item["scheduled_start"])
        body = ctx.body(item["updates"][-1], lang)
        tags = "".join(f"<li>{escape(ctx.name(c, lang))}</li>" for c in item["components"])
        text = f'<p class="mw-text">{inline_md(" ".join(body.split()))}</p>' if body else ""
        tags_label = escape(t(lang, "maintenance.components"))
        tag_list = f'<ul class="mw-tags" aria-label="{tags_label}">{tags}</ul>' if tags else ""
        rows.append(
            f"""<li class="mw mw-{state}">
  <div class="mw-date" aria-hidden="true"><span class="mw-month">{i18n.MONTHS_SHORT[lang][start.month - 1]}</span><span class="mw-day">{start.day}</span></div>
  <div class="mw-main">
    <div class="mw-head"><h3 class="mw-title"><a href="{incident_href(loc, item)}">{escape(ctx.title(item, lang))}</a></h3>
    <span class="mw-state"><span class="mw-dot" aria-hidden="true"></span>{t(lang, "update." + state)}</span></div>
    <p class="mw-window">{fmt_window(item['scheduled_start'], item['scheduled_end'], lang)}</p>
    {text}{tag_list}
  </div>
</li>"""
        )
    return f"""<section class="maintenance" aria-labelledby="maintenance-title">
  <h2 class="maintenance-title" id="maintenance-title"><svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M10.5 1.5a4 4 0 0 0-3.8 5.2L1.9 11.5a1.4 1.4 0 0 0 2 2l4.8-4.8a4 4 0 0 0 5.2-3.8l-2.2 2.2-2-.6-.6-2z"/></svg>{t(lang, "maintenance.title")}</h2>
  <ol class="mw-list">{''.join(rows)}</ol>
</section>"""


def render_index(ctx: Context, lang: str) -> str:
    loc = Loc(lang, "index.html")
    overall = ctx.overall()
    cards = "".join(render_incident_card(ctx, i, loc) for i in ctx.active if i["impact"] != "maintenance")
    groups = []
    for group in ctx.config["groups"]:
        comps = "".join(render_component(ctx, c, lang) for c in group["components"])
        live = [ctx.status_of(c) for c in group["components"]]
        group_status = worst(s for s in live if s not in ("planned", "unknown"))
        groups.append(
            f'<section class="group" aria-labelledby="g-{escape(group["name"].lower())}">'
            f'<h2 class="group-name" id="g-{escape(group["name"].lower())}"><span>{escape(group["name"])}</span>'
            f'<span class="group-status s-{group_status}">{t(lang, "status." + group_status)}</span></h2>{comps}</section>'
        )
    checked = ctx.data["status"].get("generated_at")
    checked_html = (
        f'<p class="checked">{t(lang, "index.checked")} <time datetime="{checked}">{fmt_dt(checked, lang)}</time></p>' if checked else ""
    )
    body = f"""<p class="feed-row">{feed_link(loc)}</p>
{cards}
{render_maintenance(ctx, loc)}
<div class="banner banner-{overall}" role="status"><span class="banner-icon" aria-hidden="true"></span><span>{t(lang, "banner." + overall)}</span></div>
<div class="components">
  <p class="components-intro">{t(lang, "index.intro", days=BAR_DAYS)}</p>
  {''.join(groups)}
</div>
{checked_html}
<p class="history-row"><a class="history-button" href="{loc.link("history.html")}">{t(lang, "index.history")}</a></p>"""
    return page(ctx, loc, "", body)


# --------------------------------------------------------------------------- incident pages


def render_incident_page(ctx: Context, incident: dict, lang: str) -> str:
    loc = Loc(lang, f"incidents/{incident['id']}.html")
    title = ctx.title(incident, lang)
    posted = t(lang, "incident.posted")
    rows = []
    for update in reversed(incident["updates"]):
        rows.append(
            f'<div class="update-row"><div class="update-status">{escape(ctx.update_label(update, lang))}</div>'
            f'<div class="update-text">{md_block(ctx.body(update, lang)) or "<p></p>"}'
            f'<p class="meta">{posted} <time datetime="{update["at"]}">{fmt_dt(update["at"], lang)}</time></p></div></div>'
        )
    affected = affected_names(ctx, incident, lang)
    window = ""
    if incident["impact"] == "maintenance" and incident.get("scheduled_start"):
        window = f'<p class="window">{maintenance_window(incident, lang)}</p>'
    origin = f'<p class="meta">{t(lang, "incident.auto")}</p>' if incident.get("auto") else ""
    affects = f'<p class="affects">{escape(t(lang, "affects.page", names=affected))}</p>' if affected else ""
    body = f"""<div class="top-row"><p class="crumb"><a href="{loc.link("index.html")}">&larr; {t(lang, "incident.back")}</a></p>{feed_link(loc)}</div>
<h1 class="incident-title impact-text-{incident['impact']}">{escape(title)}</h1>
<p class="impact-pill impact-{incident['impact']}">{t(lang, "impact." + incident['impact'])}</p>
{window}{origin}
<div class="updates">{''.join(rows)}</div>
{affects}"""
    return page(ctx, loc, title, body, description=t(lang, "desc.incident", title=title))


def render_history(ctx: Context, lang: str) -> str:
    loc = Loc(lang, "history.html")
    months: dict[str, list[dict]] = {}
    for incident in ctx.incidents:
        if phase(incident, ctx.now_iso) == "upcoming":
            continue
        months.setdefault(incident_start(incident)[:7], []).append(incident)
    sections = []
    for month in sorted(months, reverse=True):
        label = i18n.fmt_month(datetime.strptime(month, "%Y-%m"), lang)
        items = []
        for incident in sorted(months[month], key=incident_start, reverse=True):
            start = incident_start(incident)
            end = incident.get("resolved_at")
            if end:
                until = fmt_time(end) if end[:10] == start[:10] else fmt_dt(end, lang)
                span = f"{fmt_dt(start, lang)} {t(lang, 'history.to')} {until}"
            else:
                span = f"{fmt_dt(start, lang)} - {t(lang, 'history.ongoing')}"
            body = " ".join(ctx.body(incident["updates"][-1], lang).split())
            items.append(
                f'<li class="history-item"><h3 class="impact-text-{incident["impact"]}"><a href="{incident_href(loc, incident)}">{escape(ctx.title(incident, lang))}</a></h3>'
                f'<p>{inline_md(body)}</p><p class="meta">{span}</p></li>'
            )
        sections.append(f'<section class="month"><h2>{label}</h2><ul class="history-list">{"".join(items)}</ul></section>')
    if not sections:
        sections.append(f'<p class="quiet">{t(lang, "history.empty")}</p>')
    body = f"""<p class="crumb"><a href="{loc.link("index.html")}">&larr; {t(lang, "incident.back")}</a></p>
<h1 class="page-title">{t(lang, "history.title")}</h1>
{''.join(sections)}"""
    host = ctx.site.get("home_url", "").removeprefix("https://")
    return page(ctx, loc, t(lang, "history.title"), body, description=t(lang, "desc.history", host=host))


def render_404(ctx: Context) -> str:
    """Served for any unknown path, so it is German with an English line and links absolutely."""
    loc = Loc(DEFAULT_LANG, "404.html", absolute=True)
    body = f"""<h1 class="page-title">{t("de", "notfound.title")}</h1>
<p>{t("de", "notfound.text")} <a href="{loc.link("index.html", "de")}">{t("de", "notfound.back")}</a>.</p>
<p lang="en">{t("en", "notfound.text")} <a href="{loc.link("index.html", "en")}" hreflang="en">{t("en", "notfound.back")}</a>.</p>"""
    return page(ctx, loc, t("de", "notfound.title"), body, alternates=False)


# --------------------------------------------------------------------------- machine readable


def render_status_json(ctx: Context) -> str:
    """Language-neutral and English: keys, ids and the status description do not depend on the
    page language, so consumers of this file never see a translation."""
    overall = ctx.overall()
    components = []
    for group in ctx.config["groups"]:
        for comp in group["components"]:
            state = ctx.state(comp["id"]) or {}
            planned = comp.get("planned")
            uptime = None if planned else ctx.uptime(comp["id"])
            recent = None if planned else ctx.recent_uptime(comp["id"])
            components.append(
                {
                    "id": comp["id"],
                    "name": comp["name"],
                    "group": group["name"],
                    "status": ctx.status_of(comp),
                    "uptime_30d": round(recent, 3) if recent is not None else None,
                    "uptime_90d": round(uptime, 3) if uptime is not None else None,
                    "latency_ms": state.get("latency_ms"),
                    "checked_at": state.get("checked_at"),
                }
            )
    doc = {
        "page": {"name": ctx.site["title"], "url": ctx.site["base_url"], "updated_at": ctx.data["status"].get("generated_at")},
        "status": {"indicator": overall, "description": t("en", "banner." + overall)},
        "components": components,
        "maintenance": [
            {
                "id": i["id"],
                "title": ctx.title(i, "en"),
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
                "title": ctx.title(i, "en"),
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


def render_feed(ctx: Context, lang: str) -> str:
    """One Atom feed per language. The German one is /feed.xml, the address that existed before
    the English pages, so existing subscriptions keep working (and keep their entry ids)."""
    home = abs_url(ctx, lang, "index.html")
    entries = []
    for incident in ctx.incidents[:50]:
        url = abs_url(ctx, lang, f"incidents/{incident['id']}.html")
        maintenance = incident["impact"] == "maintenance"
        title = ctx.title(incident, lang)
        title = t(lang, "feed.maintenance", title=title) if maintenance else title
        window = ""
        if maintenance and incident.get("scheduled_start"):
            window = f"<p>{escape(maintenance_window(incident, lang))}</p>"
        content = window + "".join(
            f"<p><strong>{escape(ctx.update_label(u, lang))}</strong> ({fmt_dt(u['at'], lang)}) - {inline_md(' '.join(ctx.body(u, lang).split()))}</p>"
            for u in reversed(incident["updates"])
        )
        kind = "maintenance" if maintenance else "incident"
        entries.append(
            f"""  <entry>
    <id>{escape(url)}</id>
    <title>{escape(title)}</title>
    <category term="{kind}" label="{t(lang, 'feed.category.' + kind)}"/>
    <link rel="alternate" type="text/html" href="{escape(url)}"/>
    <published>{incident['updates'][0]['at']}</published>
    <updated>{incident['updates'][-1]['at']}</updated>
    <content type="html">{escape(content)}</content>
  </entry>"""
        )
    updated = ctx.incidents[0]["updates"][-1]["at"] if ctx.incidents else (ctx.data["status"].get("generated_at") or ctx.now_iso)
    other = OTHER[lang]
    return f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xml:lang="{lang}">
  <id>{escape(home)}</id>
  <title>{escape(t(lang, "feed.title", title=ctx.site['title']))}</title>
  <link rel="alternate" type="text/html" href="{escape(home)}"/>
  <link rel="self" type="application/atom+xml" href="{escape(abs_url(ctx, lang, "feed.xml"))}"/>
  <link rel="alternate" type="application/atom+xml" hreflang="{other}" href="{escape(abs_url(ctx, other, "feed.xml"))}"/>
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
    written = []

    def write(rel: str, text: str) -> None:
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)

    for lang in LANGS:
        write(lang_path(lang, "index.html"), render_index(ctx, lang))
        write(lang_path(lang, "history.html"), render_history(ctx, lang))
        for incident in ctx.incidents:
            write(lang_path(lang, f"incidents/{incident['id']}.html"), render_incident_page(ctx, incident, lang))
        write(lang_path(lang, "feed.xml"), render_feed(ctx, lang))
    write("404.html", render_404(ctx))
    write("status.json", render_status_json(ctx))
    host = ctx.site["base_url"].split("://", 1)[-1].strip("/")
    write("CNAME", host + "\n")
    write(".nojekyll", "")
    for asset in sorted(assets.iterdir()):
        if asset.is_file():
            shutil.copyfile(asset, out / asset.name)
            written.append(out / asset.name)
    return written
