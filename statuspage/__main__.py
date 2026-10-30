"""Command line: `python -m statuspage check|build|validate`."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import model
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


def cmd_check(args) -> int:
    config = load_config(args.config)
    settings = config["settings"]
    data = load_data(Path(args.data), Path(args.cache) if args.cache else None)
    live = [c for c in all_components(config) if not c.get("planned")]
    flat = [(comp, check) for comp in live for check in comp["checks"]]
    responses = probe_all([check for _, check in flat], settings["timeout_seconds"], settings["retry_delay_seconds"])
    results: dict[str, list[dict]] = {}
    for (comp, check), response in zip(flat, responses):
        results.setdefault(comp["id"], []).append(model.evaluate_check(check, response, settings))

    now = datetime.now(timezone.utc).replace(microsecond=0)
    data, events = model.apply_run(data, config, results, now)
    publish = args.force_publish or model.should_publish(
        events, data["status"].get("published_at"), now, settings["publish_interval_minutes"]
    )
    if publish:
        data["status"]["published_at"] = model.iso(now)
    write_data(data, Path(args.data))

    for comp in all_components(config):
        state = data["status"]["components"].get(comp["id"], {})
        detail = state.get("message") or (f"{state.get('latency_ms')} ms" if state.get("latency_ms") is not None else "")
        print(f"{comp['id']:<20} {state.get('status', '-'):<12} {detail}")
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
        }
        return commands[args.command](args)
    except (ConfigError, IncidentError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
