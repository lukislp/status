"""Command line: `python -m statuspage check|build|validate`."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import model, notify, report, schedule
from .config import ConfigError, all_components, issue_labels, load_config
from .issues import fetch_issues, items_from_issues
from .manual import IncidentError, load_manual
from .probe import probe_all
from .render import build_site

ROOT = Path(__file__).resolve().parent.parent
FILES = {"status": "status.json", "history": "history.json", "incidents": "incidents.json"}


def read_data(directory: Path) -> dict | None:
    if not all((directory / name).is_file() for name in FILES.values()):
        return None
    data = {}
    for key, name in FILES.items():
        with open(directory / name, encoding="utf-8") as fh:
            data[key] = json.load(fh)
    return data


def load_data(data_dir: Path, cache_dir: Path | None) -> dict:
    """The repository copy, unless the cache from an unpublished earlier run is newer."""
    repo = read_data(data_dir) or model.empty_data()
    cached = read_data(cache_dir) if cache_dir else None
    if cached and (cached["status"].get("generated_at") or "") > (repo["status"].get("generated_at") or ""):
        return cached
    return repo


def write_data(data: dict, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for key, name in FILES.items():
        text = json.dumps(data[key], indent=1, sort_keys=True, ensure_ascii=False) + "\n"
        (directory / name).write_text(text, encoding="utf-8", newline="\n")


def set_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def send_alerts(data: dict, config: dict, env, post=notify.urllib_post) -> list:
    """Telegram alerts for automatic incidents (notify.py); marks delivered ones in `data`.
    Never fails the run: missing secrets or a failed send only produce workflow annotations."""
    notifier = notify.from_env(env, post)
    if notifier is None:
        print("::warning title=Telegram alerts off::TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set; no alerts are sent")
        return []
    names = {c["id"]: c["name"] for c in all_components(config)}
    sent, failed = notify.dispatch(data, notifier, names, config["site"]["base_url"])
    for alert in sent:
        print(f"alert sent: {alert.kind} {alert.incident_id}")
    if failed:
        # notify.py keeps the reason (for example "HTTP 502") token-free, but only logs the count
        # here: the reason of the last failure is enough to tell Telegram trouble from a bad token.
        print(f"::warning title=Telegram alert not delivered::{len(failed)} alert(s) not delivered ({failed[-1].reason}); retried on the next run")
    return sent


def cmd_notify_test(args) -> int:
    notifier = notify.from_env(os.environ)
    if notifier is None:
        print("error: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set", file=sys.stderr)
        return 1
    try:
        notifier.send("🧪 Test: Die Alarme der Statusseite funktionieren (keine echte Störung)")
    except notify.NotifyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("test message sent")
    return 0


def cmd_check(args) -> int:
    config = load_config(args.config)
    settings = config["settings"]
    data = load_data(Path(args.data), Path(args.cache) if args.cache else None)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    live = [c for c in all_components(config) if not c.get("planned")]
    # Per check: probe it now, carry its last result forward (a check with its own interval
    # that is not due yet) or skip it (a secret it needs is not set). Results keep config order.
    planned: list[tuple[dict, dict, str, dict | None]] = []
    notes: list[str] = []
    for comp in live:
        state = data["status"]["components"].get(comp["id"])
        for check in comp["checks"]:
            action, previous = schedule.plan_check(check, state, now, os.environ)
            if action == schedule.SKIP:
                notes.append(report.skipped_line(comp["id"], check, schedule.missing_env(check, os.environ)))
                continue
            planned.append((comp, check, action, previous))
    to_probe = [(comp, check) for comp, check, action, _ in planned if action == schedule.PROBE]
    responses = probe_all(
        [check for _, check in to_probe],
        settings["timeout_seconds"],
        settings["retry_delay_seconds"],
        headers=[schedule.resolve_headers(check, os.environ) for _, check in to_probe],
    )
    fresh = {id(check): response for (_, check), response in zip(to_probe, responses)}
    results: dict[str, list[dict]] = {}
    scheduled: list[tuple[str, dict, dict]] = []
    for comp, check, action, previous in planned:
        if action == schedule.CARRY:
            result = schedule.carried_result(previous)
        else:
            result = model.evaluate_check(check, fresh[id(check)], settings)
        results.setdefault(comp["id"], []).append(result)
        if check.get("interval_minutes"):
            scheduled.append((comp["id"], check, result))

    data, events = model.apply_run(data, config, results, now)
    alerts_sent = send_alerts(data, config, os.environ)
    publish = (
        args.force_publish
        # A delivered alert is remembered only in data/, so it has to reach the repository.
        or bool(alerts_sent)
        or model.should_publish(events, data["status"].get("published_at"), now, settings["publish_interval_minutes"])
    )
    if publish:
        data["status"]["published_at"] = model.iso(now)
    write_data(data, Path(args.data))

    for comp in all_components(config):
        state = data["status"]["components"].get(comp["id"], {})
        print(report.component_line(comp["id"], state, results.get(comp["id"])))
    for cid, check, result in scheduled:
        due = schedule.next_due(check, {"checked_at": result.get("carried_from") or model.iso(now)})
        print(report.scheduled_line(cid, result, model.iso(due) if due else None))
    for note in notes:
        print(note)
    for key in ("changed", "opened", "resolved"):
        if events[key]:
            print(f"{key}: {', '.join(events[key])}")
    print(f"publish: {str(publish).lower()}")
    set_output("publish", str(publish).lower())
    return 0


def cmd_build(args) -> int:
    config = load_config(args.config)
    ids = {c["id"] for c in all_components(config)}
    manual = load_manual(args.incidents, args.maintenance, ids)
    if args.issues:
        with open(args.issues, encoding="utf-8") as fh:
            issues = json.load(fh)
        manual += items_from_issues(issues, set(config["issues"]["authors"]), ids)
    data = read_data(Path(args.data)) or model.empty_data()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    written = build_site(config, data, manual, now, Path(args.assets), Path(args.out))
    print(f"wrote {len(written)} files to {args.out}")
    return 0


def cmd_fetch_issues(args) -> int:
    config = load_config(args.config)
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = config["issues"]["repo"] or os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repo:
        print("error: GITHUB_TOKEN and issues.repo are required", file=sys.stderr)
        return 1
    issues = fetch_issues(repo, token)
    Path(args.out).write_text(json.dumps(issues, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"fetched {len(issues)} labelled issues from {repo}")
    return 0


def cmd_labels(args) -> int:
    for name, description in issue_labels(load_config(args.config)):
        print(f"{name}\t{description}")
    return 0


def cmd_validate(args) -> int:
    config = load_config(args.config)
    ids = {c["id"] for c in all_components(config)}
    items = load_manual(args.incidents, args.maintenance, ids)
    maint = sum(1 for i in items if i["impact"] == "maintenance")
    print(f"config ok: {len(ids)} components; {len(items) - maint} incident and {maint} maintenance files ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="statuspage")
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--data", default=str(ROOT / "data"))
    parser.add_argument("--incidents", default=str(ROOT / "incidents"))
    parser.add_argument("--maintenance", default=str(ROOT / "maintenance"))
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="probe every component and update data/")
    check.add_argument("--cache", help="directory with the data files of the previous, unpublished run")
    check.add_argument("--force-publish", action="store_true")
    build = sub.add_parser("build", help="render the static site")
    build.add_argument("--assets", default=str(ROOT / "site"))
    build.add_argument("--out", default=str(ROOT / "_site"))
    build.add_argument("--issues", help="JSON file written by fetch-issues (or a fixture)")
    fetch = sub.add_parser("fetch-issues", help="download incident/maintenance issues (needs GITHUB_TOKEN)")
    fetch.add_argument("--out", default=str(ROOT / ".issues.json"))
    sub.add_parser("notify-test", help="send one test message to Telegram (needs TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)")
    sub.add_parser("labels", help="print the issue labels this page understands")
    sub.add_parser("validate", help="validate config.json and incidents/*.md")
    args = parser.parse_args(argv)
    try:
        commands = {
            "check": cmd_check,
            "build": cmd_build,
            "validate": cmd_validate,
            "fetch-issues": cmd_fetch_issues,
            "labels": cmd_labels,
            "notify-test": cmd_notify_test,
        }
        return commands[args.command](args)
    except (ConfigError, IncidentError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
