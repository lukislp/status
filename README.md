# status

The status page for **[lktec.org](https://lktec.org)** and its demos, live at
**[status.lktec.org](https://status.lktec.org)**. A scheduled GitHub Actions workflow probes every
site, keeps 90 days of uptime history in this repository and publishes a static page to GitHub
Pages. Incidents and maintenance windows are posted as GitHub Issues.

[![CI](https://github.com/lukislp/status/actions/workflows/ci.yml/badge.svg)](https://github.com/lukislp/status/actions/workflows/ci.yml)
[![Check](https://github.com/lukislp/status/actions/workflows/check.yml/badge.svg)](https://github.com/lukislp/status/actions/workflows/check.yml)

No framework, no dependencies: the checker, the issue reader and the site renderer are standard
library Python. The page loads nothing from third parties, sets no cookies and has no analytics.

## What the page shows

- an active-incident card for each open incident
- planned maintenance (scheduled, in progress, completed within the last 24 hours)
- an overall banner, then every component grouped as in `config.json`, with 90 daily uptime
  bars (hover or focus a bar for date, uptime, average latency and incidents) and the 90-day
  uptime percentage
- an **Incident history** page and one page per incident

`status.json` (machine-readable summary) and `feed.xml` (Atom feed of incidents) are published
too, without links in the UI.

## How it works

```
config.json           sites, groups and checks
statuspage/
  probe.py            the only network code: GET per check, TLS expiry per host
  model.py            check evaluation, status rules, daily rollups, automatic incidents
  issues.py           GitHub Issues -> incidents / maintenance
  manual.py           incidents/*.md and maintenance/*.md (file fallback)
  render.py           static HTML, status.json, feed.xml
site/                 stylesheet, the small keyboard script for the bars, favicon
data/                 written by the checker: status.json, history.json, incidents.json
incidents/ maintenance/   optional hand-written entries (see the _template.md files)
scripts/setup-repo.sh one-time repository setup
```

**Checks.** Every check is a GET (some demos answer HEAD with 405) that can expect a status
code, a header, a piece of body text or a JSON field. A failed probe is retried once after a
few seconds. Latency and the TLS certificate's remaining days are recorded.

**Status rules.**

| Situation | Component status |
| --- | --- |
| all checks pass | Running |
| slower than `slow_ms` (5 s) or certificate expires in < `tls_warn_days` (7) | Slow |
| one failed run | Slow (a single blip is not an outage) |
| `failure_threshold` (2) failed runs in a row, every check failing | Down |
| same, but only some of the component's checks failing | Partly down |
| an open incident issue for the component | at least its impact |
| maintenance in progress for the component | Maintenance |
| `"planned": true` in the config | Coming soon (not probed) |

After `failure_threshold` failed runs an **automatic incident** is opened; it is resolved after
`recovery_threshold` (2) passing runs in a row and states how long the component was affected.

**History.** `data/history.json` holds per component and day the number of runs, the uptime
share and latency sums. Days older than `retention_days` (90) and automatic incidents resolved
more than 90 days ago are dropped, so the data stays at a few dozen kilobytes.

**Publishing.** The `Check` workflow runs every five minutes (GitHub delays scheduled runs under
load; gaps only mean fewer samples). It commits `data/` and redeploys the page when a status
changes or an incident opens/resolves, otherwise at most every `publish_interval_minutes` (30).
Runs in between carry their state forward in the Actions cache. Bot commits are SSH-signed
("Verified") and pushed with a write deploy key, using the shared
[`deploy-key-push` and `signed-bot-commit`](https://github.com/lukislp/ci-workflows) actions.
The `Pages` workflow rebuilds the page on pushes to `main` and on the owner's issue activity.

## Posting an incident

Works from the GitHub website or the GitHub mobile app.

1. **New issue → Incident.** Title = the incident title. Pick the impact (minor / major /
   critical), tick the affected components, describe what is happening. This is the first
   update ("Investigating").
2. **Comment** to post an update. Start the comment with a status word to set it:
   `Identified: ...`, `Monitoring: ...`, `Resolved: ...` (without a word it is an "Update").
3. **Close the issue** when it is over: the incident is resolved at the closing time.
   *Close as not planned* withdraws it completely (posted by mistake).

The page updates within a minute or two of each change.

**Only issues and comments by `lukislp` are used** (`config.json` → `issues.authors`). Anyone
can open an issue on a public repository and the templates attach labels automatically, so
the author check is what decides; everything from other accounts is ignored. Issue text is
escaped - no HTML, only plain text with `**bold**`, `` `code` `` and `[links](https://...)`.

Labels work as an alternative to the form fields (handy when writing a free-text issue on the
phone): `incident`, `impact:minor|major|critical`, `component:<id>`. A free-text body can also
start with lines like `impact: major` and `components: studylife-demo, piwatch-demo`.

## Posting maintenance

1. **New issue → Planned maintenance.** Enter start and end as ISO 8601 with offset, e.g.
   `2026-11-02T20:00+01:00` (or `2026-11-02 19:00` for UTC), tick the components, describe it.
2. The page shows it as *Scheduled* before the start, *In progress* during the window and
   *Completed* afterwards. Comments (optionally starting with `In-progress:`, `Verifying:`,
   `Completed:`) add updates.
3. Closing the issue completes it early; *close as not planned* cancels it.

`python -m statuspage labels` lists every label; `scripts/setup-repo.sh` creates them.

## File-based fallback

Incidents and maintenance can also be committed as Markdown, see
[`incidents/_template.md`](incidents/_template.md) and
[`maintenance/_template.md`](maintenance/_template.md). Files starting with `_` are ignored;
CI validates the others (`python -m statuspage validate`).

## Adding a component

Add an entry to a group in `config.json`:

```json
{
  "id": "newapp-demo",
  "name": "New app demo",
  "description": "Shown in the ? tooltip.",
  "checks": [
    { "name": "Web app", "url": "https://newapp-demo.lktec.org", "expect_body_contains": "New App" },
    { "name": "API", "url": "https://newapp-demo.lktec.org/api/health", "expect_json": { "ok": true } }
  ]
}
```

Check options: `expect_status` (default `200`, or a list), `expect_header` (`{"Name": "value"}`),
`expect_body_contains`, `expect_json` (top-level fields). Set `"planned": true` for a site that is
not live yet - it shows as *Coming soon* and is not probed; remove the flag once it is live (the
MowDeck demo is configured this way and already expects its `X-MowDeck-Demo: 1` header).

Then add the component to the checkbox lists in `.github/ISSUE_TEMPLATE/incident.yml` and
`maintenance.yml` (CI fails if they drift from the config) and run `scripts/setup-repo.sh` again
to create its `component:` label.

## Local development

```bash
python -m unittest discover -s tests -t .            # tests
python -m statuspage check                            # probe once, update data/
python -m statuspage build --issues tests/fixtures/issues.json
python -m http.server 8790 --bind 127.0.0.1 --directory _site
```

`build` without `--issues` renders only the file-based entries; `fetch-issues` needs a
`GITHUB_TOKEN`.

## First-time setup

The repository needs two secrets, issue labels, GitHub Pages, a ruleset and a DNS record.
`scripts/setup-repo.sh` does all of it except creating a Cloudflare record without an API token
(then it prints the record). It is idempotent and has `--dry-run`.

```bash
gh auth refresh -h github.com -s admin:ssh_signing_key   # once, if the script asks for it
scripts/setup-repo.sh --dry-run                           # show what would happen
scripts/setup-repo.sh                                     # do it
```

| Step | What |
| --- | --- |
| `DEPLOY_SIGNING_KEY` | fresh ed25519 key, registered as an account **signing** key, private part stored as the secret - bot commits show "Verified" |
| `SEMANTIC_RELEASE_DEPLOY_KEY` | fresh ed25519 **write deploy key**, private part stored as the secret - lets the bot push `data/` past the ruleset |
| labels | `incident`, `maintenance`, `impact:*`, `component:*` |
| Pages | source "GitHub Actions", custom domain `status.lktec.org`, HTTPS enforced (only possible once DNS works - run the script again later) |
| ruleset | copy of `lukislp/demosites`' "main protection": PRs, required checks `test` + `lint`, signed commits, no force pushes/deletion; bypass: deploy keys (always), admins (pull requests) |
| DNS | with `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ZONE_ID` set: CNAME `status` → `lukislp.github.io`, **DNS only** (GitHub needs to issue the certificate); otherwise it prints the record |
| `AUTOMERGE_TOKEN` | optional prompt for the PAT used by Dependabot auto-merge (`--no-automerge` skips) |

Private keys only exist in a temporary directory during the run and are deleted afterwards.
`--rotate-keys` replaces both keys.

## License

[MIT](LICENSE)
