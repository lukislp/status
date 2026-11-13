"""Every user-facing string of the page, in German (the default, served at /) and English
(served under /en/), plus the date, number and duration formats of each language.

Pure data and tiny helpers, no imports from the rest of the package, so the model, the renderer
and the tests can all use it. `STRINGS` has one table per language; a test fails when a key is
missing in either, so a new string cannot ship in one language only.

Terminology (German): Störung = incident, Wartung = maintenance, Verfügbarkeit = uptime,
Komponente = component. Wording is impersonal ("Es gibt ..." instead of "Wir ...").
"""

from __future__ import annotations

from datetime import datetime

DEFAULT_LANG = "de"
LANGS = ("de", "en")
OTHER = {"de": "en", "en": "de"}

MONTHS_SHORT = {
    "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
    "de": ["Jan.", "Feb.", "März", "Apr.", "Mai", "Juni", "Juli", "Aug.", "Sep.", "Okt.", "Nov.", "Dez."],
}
MONTHS_LONG = {
    "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September",
           "October", "November", "December"],
    "de": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September",
           "Oktober", "November", "Dezember"],
}
WEEKDAYS_SHORT = {
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    "de": ["Mo.", "Di.", "Mi.", "Do.", "Fr.", "Sa.", "So."],
}

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        # component status and overall banner
        "status.operational": "Running",
        "status.maintenance": "Maintenance",
        "status.degraded": "Slow",
        "status.partial": "Partly down",
        "status.major": "Down",
        "status.planned": "Coming soon",
        "status.unknown": "No data yet",
        "banner.operational": "Everything is up and running",
        "banner.maintenance": "Planned maintenance is underway",
        "banner.degraded": "Some services are slower than usual",
        "banner.partial": "Some services are partly unavailable",
        "banner.major": "A service is currently down",
        # incident and maintenance updates
        "update.investigating": "Investigating",
        "update.identified": "Identified",
        "update.monitoring": "Monitoring",
        "update.update": "Update",
        "update.resolved": "Resolved",
        "update.scheduled": "Scheduled",
        "update.in-progress": "In progress",
        "update.verifying": "Verifying",
        "update.completed": "Completed",
        "impact.none": "No impact",
        "impact.minor": "Minor",
        "impact.major": "Major",
        "impact.critical": "Critical",
        "impact.maintenance": "Maintenance",
        # page chrome
        "skip": "Skip to content",
        "brand.home": "{title} home",
        "lang.nav": "Language",
        "lang.de": "Deutsch",
        "lang.en": "English",
        "footer.nav": "Footer",
        "footer.history": "Incident history",
        "footer.source": "Source",
        "footer.imprint": "Imprint",
        "footer.privacy": "Privacy",
        "footer.note": "Hosted on GitHub Pages, which logs visitor IP addresses for security purposes. This page loads nothing from third parties, sets no cookies and uses no analytics.",
        "footer.privacy_see": "See the {link}.",
        "footer.privacy_link": "privacy policy",
        "feed.subscribe": "Subscribe (Atom)",
        "feed.alt": "{title} incidents",
        "feed.title": "{title} - incidents",
        "feed.maintenance": "Maintenance: {title}",
        "feed.category.maintenance": "Maintenance",
        "feed.category.incident": "Incident",
        "desc.index": "Live status, uptime and incident history for {host} and its demos.",
        "desc.incident": "Incident report: {title}",
        "desc.history": "Past incidents and maintenance of {host} and its demos.",
        # index
        "index.intro": "Uptime over the past {days} days.",
        "index.checked": "Last checked",
        "index.history": "Incident history",
        "maintenance.title": "Planned maintenance",
        "maintenance.components": "Affected components",
        "maintenance.window": "Scheduled for {start} to {end}",
        "affects.card": "This affects: {names}.",
        "affects.page": "This incident affected: {names}.",
        "component.bars": "Daily uptime of {name} over the past {days} days, oldest first. Use the arrow keys to move between days.",
        "legend.90": "90 days ago",
        "legend.60": "60 days ago",
        "legend.30": "30 days ago",
        "legend.today": "Today",
        "uptime.none": "No data yet",
        "uptime.pct": "{value} % uptime",
        "uptime.not_live": "Not live yet",
        "bar.no_data": "No data",
        "bar.avg": "avg {ms} ms",
        "bar.no_incidents": "No incidents",
        "bar.no_incidents_text": "no incidents",
        # incident page, history, 404
        "incident.back": "Current status",
        "incident.posted": "Posted",
        "incident.auto": "Opened automatically by the uptime checks.",
        "history.title": "Incident history",
        "history.empty": "No incidents recorded yet.",
        "history.to": "to",
        "history.ongoing": "ongoing",
        "notfound.title": "Page not found",
        "notfound.text": "That page does not exist.",
        "notfound.back": "Back to the current status",
        # text generated by the code (automatic incidents, closed issues)
        "auto.title.major": "{name} is unavailable",
        "auto.title.partial": "{name} is partially unavailable",
        "auto.opened": "Automated checks for {name} are failing.",
        "auto.where": "Where it failed - {parts}.",
        "auto.worse": "The problem got worse.",
        "auto.resolved": "Automated checks for {name} are passing again. The component was affected for about {duration}.",
        "issue.resolved": "This incident has been resolved.",
        "issue.completed": "The maintenance is complete.",
        "duration.min": "{m} min",
        "duration.hm": "{h} h {m} min",
        "duration.h": "{h} h",
        "duration.days": "{d} days",
        # where a check failed (diagnosis.PHASE_TEXT)
        "phase.dns": "the name could not be resolved",
        "phase.connect": "no connection could be opened",
        "phase.tls": "the secure connection could not be set up",
        "phase.timeout_response": "connected, but no response arrived in time",
        "phase.response_error": "connected, but the connection broke before a complete response",
        "phase.http_status": "the server answered with an unexpected status",
        "phase.body": "the server answered, but not with the expected content",
        "phase.slow": "the server answered, but slowly",
    },
    "de": {
        "status.operational": "Betriebsbereit",
        "status.maintenance": "Wartung",
        "status.degraded": "Langsam",
        "status.partial": "Teilweise gestört",
        "status.major": "Ausfall",
        "status.planned": "Demnächst",
        "status.unknown": "Noch keine Daten",
        "banner.operational": "Alle Dienste sind betriebsbereit",
        "banner.maintenance": "Geplante Wartung läuft",
        "banner.degraded": "Einige Dienste sind langsamer als üblich",
        "banner.partial": "Einige Dienste sind teilweise gestört",
        "banner.major": "Ein Dienst ist derzeit ausgefallen",
        "update.investigating": "In Untersuchung",
        "update.identified": "Ursache gefunden",
        "update.monitoring": "Wird beobachtet",
        "update.update": "Aktualisierung",
        "update.resolved": "Behoben",
        "update.scheduled": "Geplant",
        "update.in-progress": "In Durchführung",
        "update.verifying": "Wird geprüft",
        "update.completed": "Abgeschlossen",
        "impact.none": "Keine Auswirkung",
        "impact.minor": "Gering",
        "impact.major": "Erheblich",
        "impact.critical": "Kritisch",
        "impact.maintenance": "Wartung",
        "skip": "Zum Inhalt springen",
        "brand.home": "{title} Startseite",
        "lang.nav": "Sprache",
        "lang.de": "Deutsch",
        "lang.en": "English",
        "footer.nav": "Fußbereich",
        "footer.history": "Störungshistorie",
        "footer.source": "Quellcode",
        "footer.imprint": "Impressum",
        "footer.privacy": "Datenschutz",
        "footer.note": "Gehostet auf GitHub Pages, das IP-Adressen der Besucher aus Sicherheitsgründen protokolliert. Diese Seite lädt nichts von Dritten, setzt keine Cookies und verwendet keine Analyse-Werkzeuge.",
        "footer.privacy_see": "Siehe die {link}.",
        "footer.privacy_link": "Datenschutzerklärung",
        "feed.subscribe": "Abonnieren (Atom)",
        "feed.alt": "{title} Störungen",
        "feed.title": "{title} - Störungen",
        "feed.maintenance": "Wartung: {title}",
        "feed.category.maintenance": "Wartung",
        "feed.category.incident": "Störung",
        "desc.index": "Live-Status, Verfügbarkeit und Störungshistorie für {host} und die zugehörigen Demos.",
        "desc.incident": "Störungsbericht: {title}",
        "desc.history": "Frühere Störungen und Wartungen von {host} und den zugehörigen Demos.",
        "index.intro": "Verfügbarkeit der letzten {days} Tage.",
        "index.checked": "Zuletzt geprüft",
        "index.history": "Störungshistorie",
        "maintenance.title": "Geplante Wartung",
        "maintenance.components": "Betroffene Komponenten",
        "maintenance.window": "Geplant von {start} bis {end}",
        "affects.card": "Betroffen: {names}.",
        "affects.page": "Von dieser Störung betroffen: {names}.",
        "component.bars": "Tägliche Verfügbarkeit von {name} in den letzten {days} Tagen, älteste zuerst. Mit den Pfeiltasten zwischen den Tagen wechseln.",
        "legend.90": "vor 90 Tagen",
        "legend.60": "vor 60 Tagen",
        "legend.30": "vor 30 Tagen",
        "legend.today": "Heute",
        "uptime.none": "Noch keine Daten",
        "uptime.pct": "{value} % Verfügbarkeit",
        "uptime.not_live": "Noch nicht live",
        "bar.no_data": "Keine Daten",
        "bar.avg": "Ø {ms} ms",
        "bar.no_incidents": "Keine Störungen",
        "bar.no_incidents_text": "keine Störungen",
        "incident.back": "Aktueller Status",
        "incident.posted": "Veröffentlicht",
        "incident.auto": "Automatisch durch die Verfügbarkeitsprüfungen eröffnet.",
        "history.title": "Störungshistorie",
        "history.empty": "Bisher wurden keine Störungen erfasst.",
        "history.to": "bis",
        "history.ongoing": "andauernd",
        "notfound.title": "Seite nicht gefunden",
        "notfound.text": "Diese Seite existiert nicht.",
        "notfound.back": "Zurück zum aktuellen Status",
        "auto.title.major": "{name} ist nicht verfügbar",
        "auto.title.partial": "{name} ist teilweise nicht verfügbar",
        "auto.opened": "Die automatischen Prüfungen für {name} schlagen fehl.",
        "auto.where": "Fehlerstelle - {parts}.",
        "auto.worse": "Das Problem hat sich verschärft.",
        "auto.resolved": "Die automatischen Prüfungen für {name} sind wieder erfolgreich. Die Komponente war etwa {duration} beeinträchtigt.",
        "issue.resolved": "Diese Störung wurde behoben.",
        "issue.completed": "Die Wartung ist abgeschlossen.",
        "duration.min": "{m} Min.",
        "duration.hm": "{h} Std. {m} Min.",
        "duration.h": "{h} Std.",
        "duration.days": "{d} Tage",
        "phase.dns": "der Name konnte nicht aufgelöst werden",
        "phase.connect": "es konnte keine Verbindung aufgebaut werden",
        "phase.tls": "die gesicherte Verbindung konnte nicht aufgebaut werden",
        "phase.timeout_response": "verbunden, aber es kam keine Antwort innerhalb der Zeitgrenze",
        "phase.response_error": "verbunden, aber die Verbindung brach vor einer vollständigen Antwort ab",
        "phase.http_status": "der Server antwortete mit einem unerwarteten Status",
        "phase.body": "der Server antwortete, aber nicht mit dem erwarteten Inhalt",
        "phase.slow": "der Server antwortete, aber langsam",
    },
}


def t(lang: str, key: str, **values) -> str:
    text = STRINGS[lang][key]
    return text.format(**values) if values else text


# --------------------------------------------------------------------------- formats


def fmt_dt(dt: datetime, lang: str) -> str:
    """'Nov 13, 2026 - 21:05 UTC' / '13.11.2026, 21:05 UTC'."""
    if lang == "de":
        return f"{dt.day}.{dt.month:02d}.{dt.year}, {dt:%H:%M} UTC"
    return f"{MONTHS_SHORT['en'][dt.month - 1]} {dt.day}, {dt.year} - {dt:%H:%M} UTC"


def fmt_day(dt: datetime, lang: str) -> str:
    if lang == "de":
        return f"{dt.day}.{dt.month:02d}.{dt.year}"
    return f"{MONTHS_SHORT['en'][dt.month - 1]} {dt.day}, {dt.year}"


def fmt_month(dt: datetime, lang: str) -> str:
    return f"{MONTHS_LONG[lang][dt.month - 1]} {dt.year}"


def fmt_short_date(dt: datetime, lang: str) -> str:
    """'Mon, Nov 2' / 'Mo., 2. Nov.' (weekday, day, month)."""
    weekday = WEEKDAYS_SHORT[lang][dt.weekday()]
    if lang == "de":
        return f"{weekday}, {dt.day}. {MONTHS_SHORT['de'][dt.month - 1]}"
    return f"{weekday}, {MONTHS_SHORT['en'][dt.month - 1]} {dt.day}"


def fmt_number(value: float, decimals: int, lang: str) -> str:
    text = f"{value:.{decimals}f}"
    return text.replace(".", ",") if lang == "de" else text


def fmt_duration(minutes: int, lang: str) -> str:
    if minutes < 60:
        return t(lang, "duration.min", m=max(minutes, 1))
    hours, rest = divmod(minutes, 60)
    if hours < 48:
        return t(lang, "duration.hm", h=hours, m=rest) if rest else t(lang, "duration.h", h=hours)
    return t(lang, "duration.days", d=hours // 24)


# --------------------------------------------------------------------------- generated text


def where_sentence(lang: str, parts: list) -> str:
    """'Where it failed - Web app: ...; API: ....' from [[check name, phase], ...]."""
    texts = [f"{name}: {t(lang, 'phase.' + phase)}" for name, phase in parts if f"phase.{phase}" in STRINGS[lang]]
    return t(lang, "auto.where", parts="; ".join(texts)) if texts else ""


def render_tpl(lang: str, tpl: dict, name_of) -> str:
    """The text of an automatic incident or a generated update, from its stored template
    (`tpl` on the incident or update). `name_of(component id)` gives the component's name in
    `lang`. The English result is also what is stored as the plain `title` / `body`."""
    key = tpl["key"]
    if key == "auto_title":
        return t(lang, f"auto.title.{tpl['kind']}", name=name_of(tpl["component"]))
    if key == "auto_opened":
        message, where = tpl.get("message", ""), where_sentence(lang, tpl.get("where") or [])
        detail = f"{message}. {where}" if message and where else message or where
        return f"{t(lang, 'auto.opened', name=name_of(tpl['component']))} {detail}".strip()
    if key == "auto_worse":
        return f"{t(lang, 'auto.worse')} {tpl.get('message', '')}".strip()
    if key == "auto_resolved":
        return t(lang, "auto.resolved", name=name_of(tpl["component"]), duration=fmt_duration(tpl["minutes"], lang))
    if key in ("issue_resolved", "issue_completed"):
        return t(lang, "issue." + key.split("_", 1)[1])
    raise KeyError(key)
