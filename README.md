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

**Languages.** The page is German at `/` and English under `/en/` (same structure: `index.html`,
`history.html`, `incidents/<id>.html`, `feed.xml`). Every page has a DE | EN switch to the same page in
the other language, `hreflang` alternates (German is `x-default`) and a canonical URL. All fixed
texts, date formats (German: `13.11.2026, 21:05 UTC`) and the wording of automatic incidents live in
`statuspage/i18n.py`; a test fails when a text exists in one language only. Component names and
descriptions are English in `config.json` with optional German `name_de` / `description_de`
(missing German text falls back to English). Incident and maintenance texts you write yourself are
shown as written in both languages; the automatic incidents' sentences are generated in both. The
check messages inside them (for example `HTTP 503`) stay as the checker wrote them.

`status.json` (machine-readable summary with `uptime_30d` and `uptime_90d` per component, in percent, and
`page.updated_at` as the freshness stamp) is published too, without a link in the UI. It exists once,
at `/status.json`, and is not translated (English descriptions, ids as keys). `feed.xml` (Atom feed of
automatic and manual incidents and maintenance announcements, the latter titled "Wartung: ..." /
"Maintenance: ...") exists per language, `/feed.xml` (German) and `/en/feed.xml`, and is linked as
"Abonnieren (Atom)" / "Subscribe (Atom)" at the top of the main page and of every incident page.

## How it works

```
config.json           sites, groups and checks
statuspage/
  probe.py            the only network code: one request per check (with timings), TLS expiry per host
  schedule.py         pure: which checks this run probes, carries forward or skips
  diagnosis.py        pure: where a failed or slow check failed (phase), Cloudflare ids
  model.py            check evaluation, status rules, daily rollups, automatic incidents
  report.py           the workflow log lines (phase, timings, cf-ray)
  issues.py           GitHub Issues -> incidents / maintenance
  manual.py           incidents/*.md and maintenance/*.md (file fallback)
  notify.py           Telegram alerts for automatic incidents (send-only), exactly-once markers
  i18n.py             pure: every user-facing text in German and English, date and duration formats
  render.py           static HTML (German at /, English under /en/), status.json, one feed.xml per language
site/                 stylesheet, the small keyboard script for the bars, favicon
data/                 written by the checker: status.json, history.json, incidents.json
incidents/ maintenance/   optional hand-written entries (see the _template.md files)
scripts/setup-repo.sh one-time repository setup
```

**Checks.** Every check is a GET (some demos answer HEAD with 405), or a POST with an empty
body where the config says so, that can expect a status code, a header, a piece of body text or
a JSON field. A failed probe is retried once after a few seconds. Latency and the TLS
certificate's remaining days are recorded.

**Checks with their own interval.** A check with `interval_minutes` is probed only when its last
result is at least that old; the runs in between carry that result forward (it keeps counting for
the component, and `data/status.json` keeps when it was probed). A check without a previous result
is due at once, so a newly added check reports in the first run after the merge. The chat
assistant's answer probe uses this: it asks the model a real question, so it runs once a day while
the cheap health check runs every five minutes. A check whose `headers_from_env` names an
environment variable that is not set (a missing repository secret) is skipped with a warning
annotation in the run, and the component's other checks still run.

**What a check records.** Besides the verdict, every probe keeps *where* a failed or slow check
failed (the **phase**) and cheap timings, so that a failure can be attributed from the check run
alone:

| Phase | Meaning |
| --- | --- |
| `dns` | the name did not resolve |
| `connect` | TCP connection refused, unreachable or timed out |
| `tls` | handshake or certificate failed (a timeout during the handshake counts here) |
| `timeout_response` | connected, but no response headers within `timeout_seconds` |
| `response_error` | connected, but the connection broke (or the body could not be read) |
| `http_status` | the server answered with an unexpected status |
| `body` | the answer lacked the expected header, text or JSON field |
| `slow` | the answer was fine but slower than `slow_ms` |

Timings: TCP connect, TLS handshake, time to the first response byte and the total, in ms. From
the response, `cf-ray` and `cf-cache-status` (Cloudflare's request id and cache result) are kept;
no other header, no body and no address is recorded. The phase is decided in one pure function
(`diagnosis.classify_phase`) from how far the request got and the exception that stopped it, so it
is testable without a network. Limits: with the standard library a check cannot tell a refused
connection from a firewall that drops packets (both end as `connect`, the latter after the full
timeout), nor a slow origin from a slow Cloudflare edge; the ray id is what tells Cloudflare which
request to look up.

**Reading a failed check in the workflow log.** The `Probe components` step prints one line per
component. A problem is followed by its phase, timings and ids; a successful check shows its ray id:

```
piwatch-demo         degraded     Web app: timed out (phase: timeout_response, connect 43 ms, tls 61 ms, total 15062 ms; no response, so no cf-ray)
studylife-demo       degraded     Web app: HTTP 502 (attempt 1: phase: timeout_response, connect 40 ms, tls 60 ms, total 15000 ms; no response, so no cf-ray; attempt 2: phase: http_status, connect 41 ms, tls 59 ms, first byte 12 ms, total 118 ms; cf-ray 9a1b2c3d4e5f6a7b-FRA)
mowdeck-demo         operational  618 ms (cf-ray 9a1b2c3d4e5f6a7b-FRA, cf-cache-status DYNAMIC)
```

A check with its own interval gets one more line: the outcome of this run's probe (HTTP status,
latency and the JSON fields named in `report_json_fields`) or which earlier result is carried and
when the next probe is due. Request headers (the probe token) are never printed:

```
portfolio-chat                    Answer probe: probed now, ok (HTTP 200; 3412 ms; provider=anthropic, ttftMs=912, latencyMs=3120)
portfolio-chat                    Answer probe: ok (probed 2026-11-14T08:05:12Z, next probe at 2026-11-15T08:05:12Z)
```

A failed probe is retried once, and then both attempts are shown because they can differ. A
`timeout_response` without a ray id means the request never got an answer from Cloudflare's side
either; if a retry shows a ray id (or the site was reachable meanwhile), the ray id of the failing
request is what to hand to Cloudflare support.

**What is published.** The log is the only place for timings and `cf-ray` ids. `data/status.json`
(committed to this repository) holds the phase of every check, and the text of an automatic incident
adds one sentence, for example *"Where it failed - Web app: connected, but no response arrived in
time."* The published `status.json`, the pages and `data/history.json` carry no phase, timing or
id; the history format is unchanged. Data files written before the phase existed still load.

**Status rules.**

| Situation | Component status |
| --- | --- |
| all checks pass | Running |
| slower than `slow_ms` (5 s) or certificate expires in < `tls_warn_days` (7) | Slow |
| a check's answer matches its `degraded_json` (the chat answered from its fallback model) | Slow |
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
Runs in between carry their state forward in the Actions cache. The commit is made by the
[lktec-bot](https://github.com/lukislp/lktec-bot) GitHub App through `createCommitOnBranch`
(module `sync`): the `check` job hands the changed files of `data/` over as an artifact, the
`sync` job commits them, and GitHub itself signs the commit ("Verified"). The App is a bypass
actor of the `main` ruleset and the policy in `.github/lktec-bot.yml` limits it to `data/`.
The `Pages` workflow rebuilds the page on pushes to `main` and on the owner's issue activity.

## Telegram alerts

Alerts are German and link to the **German** incident page (`/incidents/<id>.html`).

When an **automatic** incident opens (a component is *Partly down* or *Down* after
`failure_threshold` failed runs) the checker sends one Telegram message; when it resolves, one
"Wieder da" message. If the incident later gets worse (*Partly down* to *Down*) it sends one more.
No alert for *Slow*, and none for incidents or maintenance you post yourself.

Setup (once):

1. Talk to [@BotFather](https://t.me/BotFather), `/newbot`, and keep the token.
2. Send any message to the new bot (or add it to a group), then open
   `https://api.telegram.org/bot<token>/getUpdates` and read `chat.id` from the result.
3. Add the repository secrets `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
4. Run the **Check** workflow manually with `test_alert` ticked: it sends one message marked as a
   test, and fails with a readable error if a secret is missing or Telegram refuses the request.

Without both secrets the check step only prints a warning annotation and sends nothing.

The bot is send-only. Every message goes to the single chat in `TELEGRAM_CHAT_ID` through
`sendMessage`; the code never calls `getUpdates`, never reads incoming messages, keeps no subscriber
list, handles no commands such as `/start` and sets no webhook. Anyone who presses Start in the bot
receives nothing. (The `getUpdates` call in step 2 is a one-off by hand to find your chat id.)
A Telegram outage (for example HTTP 502) or a wrong token only produces a token-free warning
annotation, and the alert is retried on the next run.

Exactly once: what is due is derived from `data/incidents.json`. An incident gets `alerted`
(and `alerted_impact`) after its alert was delivered, and `recovery_alerted` after the recovery
alert. The marker is stored only after Telegram answered 200, so a failed send is retried on the
next run (every five minutes) and a delivered one is never repeated. A delivered alert also
forces a publish, so the marker reaches the repository and survives a lost Actions cache. A
recovery alert is only sent for incidents whose "down" alert was delivered. The bot token is part
of the Telegram URL, so error texts are redacted and the token is never printed or stored.

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
  "name_de": "Neue App-Demo",
  "description_de": "Wird im ?-Tooltip der deutschen Seite gezeigt.",
  "checks": [
    { "name": "Web app", "url": "https://newapp-demo.lktec.org", "expect_body_contains": "New App" },
    { "name": "API", "url": "https://newapp-demo.lktec.org/api/health", "expect_json": { "ok": true } }
  ]
}
```

`name_de` and `description_de` are optional (non-empty strings); the German page uses them and
falls back to `name` / `description`. Imprint and privacy links of the German page can be set with
`site.imprint_url_de` / `site.privacy_url_de`.

Check options: `expect_status` (default `200`, or a list), `expect_header` (`{"Name": "value"}`),
`expect_body_contains`, `expect_json` (top-level fields).

For a check that is more than a GET every run:

| Option | Meaning |
| --- | --- |
| `method` | `GET` (default) or `POST` (sent with an empty body) |
| `headers_from_env` | `{"Header-Name": "ENV_VAR"}`: request headers whose values come from the environment (repository secrets passed in `check.yml`); never printed or stored. Unset variable: the check is skipped with a warning |
| `interval_minutes` | probe at most this often (at least 5) and carry the last result forward in between; needs a `name` unique in the component |
| `timeout_seconds`, `slow_ms` | this check's own timeout and slow threshold instead of the settings |
| `degraded_json` | top-level fields that mark a passing answer as degraded (shown as *Slow*, counts as up); `degraded_note` is the text for it |
| `report_json_fields` | top-level JSON fields printed in the workflow log line (never published) |

The chat assistant component (`portfolio-chat`) uses them: `GET /api/chat/health` every run, and
once a day `POST /api/chat/probe` with the `X-Status-Probe` header from the repository secret
`CHAT_PROBE_TOKEN` (the same value as `CHAT_PROBE_TOKEN` in the Cloudflare Pages production
environment of lukislp/portfolio, which documents both endpoints). The probe answers
`{"ok":true,"provider":...}`, with `"degraded":true` when the fallback model answered; a 503 is a
failure and counts by `failure_threshold` like any other. Set `"planned": true` for a site that is
not live yet - it shows as *Coming soon* and is not probed; remove the flag once it is live.

`maintenance_response` (`{"status": 503, "header": {...}, "body_contains": "..."}`) declares a
known, planned answer that counts as up. Every given part must match, and a header or body text
is required, so any other 503, a 502 or a timeout is still an outage. The MowDeck demo uses it
for its reset every six hours (Caddy answers 503, `Retry-After: 120`, "The MowDeck demo is being
reset ...").

## Correcting a day's uptime

`"uptime_resets": ["YYYY-MM-DD"]` in `config.json` is a one-off correction: the next checker run
marks every run of that day as up for all components and records the day in
`history.json` → `resets_applied`, so it is applied exactly once - on the repository copy or the
Actions cache copy, whichever is newer - and later runs of that day count normally again. It
was used for 2026-10-31 (planned host maintenance of the demo server).

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

The repository needs the two lktec-bot App secrets, issue labels, GitHub Pages, a ruleset and a DNS record.
`scripts/setup-repo.sh` does all of it except creating a Cloudflare record without an API token
(then it prints the record). It is idempotent and has `--dry-run`.

```bash
scripts/setup-repo.sh --dry-run                           # show what would happen
scripts/setup-repo.sh                                     # do it
```

| Step | What |
| --- | --- |
| `LKTEC_BOT_APP_ID` | secret, ID of the lktec-bot GitHub App (installed on the repository) - the script only checks that it exists |
| `LKTEC_BOT_PRIVATE_KEY` | secret, private key of the App - the script only checks that it exists; with these two the App commits `data/` and GitHub signs the commit |
| `CHAT_PROBE_TOKEN` | secret, set by hand: the chat assistant's probe token (same value as in Cloudflare Pages); without it the daily answer probe is skipped with a warning |
| labels | `incident`, `maintenance`, `impact:*`, `component:*` |
| Pages | source "GitHub Actions", custom domain `status.lktec.org`, HTTPS enforced (only possible once DNS works - run the script again later) |
| ruleset | copy of `lukislp/demosites`' "main protection": PRs, required checks `test` + `lint`, signed commits, no force pushes/deletion; bypass: the lktec-bot App (always), admins (pull requests) |
| DNS | with `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ZONE_ID` set: CNAME `status` → `lukislp.github.io`, **DNS only** (GitHub needs to issue the certificate); otherwise it prints the record |
| `AUTOMERGE_TOKEN` | optional prompt for the PAT used by Dependabot auto-merge (`--no-automerge` skips) |

Private keys only exist in a temporary directory during the run and are deleted afterwards.
`--rotate-keys` replaces both keys.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md). Security problems: [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
