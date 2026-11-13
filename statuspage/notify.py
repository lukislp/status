"""Alerts for automatic incidents: which messages are due (pure), and how they are delivered.

`pending_alerts` reads only data/incidents.json and the component states, so what has to be sent
is derived from persisted state. An alert counts as delivered only once its marker is stored on the
incident (`mark_sent`); the checker does that after a successful send and before it writes data/.
A failed send leaves no marker, so the next run tries again, and a delivered one is never repeated.

Delivery goes through the `Notifier` port. `TelegramNotifier` is the only adapter; its HTTP sender
is injected, so tests never touch the network. The module is send-only: it calls sendMessage with
the one configured chat id and never reads updates, keeps subscribers or handles commands. The bot token is part of the request URL, so it is
redacted from every error text this module produces.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from typing import Callable, Protocol

from .model import IMPACTS, parse_iso

OPENED = "opened"
ESCALATED = "escalated"
RECOVERED = "recovered"

# Only these impacts page the owner: "Partly down" (major) and "Down" (critical). A slow or
# degraded component never opens an automatic incident, and never gets an alert.
ALERT_IMPACTS = {"major": ("🟠", "Partly down"), "critical": ("🔴", "Down")}
MESSAGE_MAX = 200
TELEGRAM_MAX = 4096
API_URL = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT_SECONDS = 10


class NotifyError(Exception):
    """A delivery failure. The text never contains the bot token."""


class Notifier(Protocol):
    def send(self, text: str) -> None:
        """Deliver one message, or raise NotifyError."""


@dataclass(frozen=True)
class Alert:
    kind: str
    incident_id: str
    text: str
    impact: str
    reason: str = ""  # why the send failed (token-free); set only on alerts returned as failed


# --------------------------------------------------------------------------- what is due


def _duration(minutes: int) -> str:
    if minutes < 60:
        return f"{max(minutes, 1)} Min."
    hours, rest = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} Std. {rest} Min." if rest else f"{hours} Std."
    return f"{hours // 24} Tagen"


def _clip(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= MESSAGE_MAX else text[: MESSAGE_MAX - 1] + "…"


def pending_alerts(data: dict, names: dict[str, str], base_url: str) -> list[Alert]:
    """Alerts that are due, oldest incident first. Only automatic incidents take part."""
    base = base_url.rstrip("/")
    states = data["status"]["components"]
    due: list[Alert] = []
    for incident in data["incidents"]["incidents"]:
        if not incident.get("auto"):
            continue
        cid = incident["components"][0] if incident.get("components") else ""
        name = names.get(cid, cid or incident["title"])
        url = f"{base}/incidents/{incident['id']}.html"
        impact = incident["impact"]
        if incident.get("resolved_at"):
            # Recovery only after the matching "down" alert was delivered; a problem the owner
            # never heard about needs no "all clear".
            if incident.get("alerted") and not incident.get("recovery_alerted"):
                span = parse_iso(incident["resolved_at"]) - parse_iso(incident["created_at"])
                text = f"🟢 Wieder da: {name} nach {_duration(int(span.total_seconds() // 60))}\n{url}"
                due.append(Alert(RECOVERED, incident["id"], text, impact))
            continue
        if impact not in ALERT_IMPACTS:
            continue
        icon, label = ALERT_IMPACTS[impact]
        message = _clip((states.get(cid) or {}).get("message", ""))
        since = parse_iso(incident["created_at"]).strftime("%H:%M")
        detail = f" – {message}" if message else ""
        if not incident.get("alerted"):
            text = f"{icon} {label}: {name}{detail} – seit {since} UTC\n{url}"
            due.append(Alert(OPENED, incident["id"], text, impact))
        elif IMPACTS.index(impact) > IMPACTS.index(incident.get("alerted_impact") or impact):
            # One update when "Partly down" turns into "Down"; never a repeat at the same level.
            text = f"{icon} Schlimmer, jetzt {label}: {name}{detail} – seit {since} UTC\n{url}"
            due.append(Alert(ESCALATED, incident["id"], text, impact))
    return due


def mark_sent(data: dict, alert: Alert) -> None:
    """Record a delivered alert on its incident (the only thing this module writes to data/)."""
    for incident in data["incidents"]["incidents"]:
        if incident["id"] != alert.incident_id:
            continue
        if alert.kind == RECOVERED:
            incident["recovery_alerted"] = True
        else:
            incident["alerted"] = True
            incident["alerted_impact"] = alert.impact
        return


def dispatch(data: dict, notifier: Notifier, names: dict[str, str], base_url: str) -> tuple[list[Alert], list[Alert]]:
    """Send every due alert and mark the delivered ones in `data`. Never raises for a send
    failure. Returns (sent, failed); `failed` ones are retried by the next run."""
    sent: list[Alert] = []
    failed: list[Alert] = []
    for alert in pending_alerts(data, names, base_url):
        try:
            notifier.send(alert.text)
        except NotifyError as exc:
            failed.append(replace(alert, reason=str(exc)))
            continue
        mark_sent(data, alert)
        sent.append(alert)
    return sent, failed


# --------------------------------------------------------------------------- Telegram adapter

# post(url, body) -> (HTTP status, response text); raises OSError for transport problems.
Post = Callable[[str, bytes], "tuple[int, str]"]


def urllib_post(url: str, body: bytes) -> tuple[int, str]:
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, post: Post = urllib_post):
        self._token = token
        self._chat_id = chat_id
        self._post = post

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    def send(self, text: str) -> None:
        body = json.dumps(
            {"chat_id": self._chat_id, "text": text[:TELEGRAM_MAX], "disable_web_page_preview": True}
        ).encode("utf-8")
        try:
            status, answer = self._post(API_URL.format(token=self._token), body)
        except Exception as exc:  # transport errors can quote the URL, which holds the token
            raise NotifyError(self._redact(f"Telegram request failed ({type(exc).__name__})")) from None
        if status == 200:
            return
        try:
            reason = str(json.loads(answer).get("description", ""))
        except (ValueError, AttributeError):
            reason = ""
        raise NotifyError(self._redact(f"Telegram answered HTTP {status}" + (f": {_clip(reason)}" if reason else "")))


def from_env(env, post: Post = urllib_post) -> TelegramNotifier | None:
    token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (env.get("TELEGRAM_CHAT_ID") or "").strip()
    return TelegramNotifier(token, chat_id, post) if token and chat_id else None
