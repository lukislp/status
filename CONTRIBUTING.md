# Contributing

This is a small, stdlib-only Python status page (`statuspage/`), published on GitHub Pages at https://status.lktec.org. It has one maintainer, so the process is deliberately small, and it is the same for every change, the maintainer's own included. Security problems never go into a public issue: see [SECURITY.md](SECURITY.md). Everyone taking part follows the [Code of Conduct](CODE_OF_CONDUCT.md).

## How changes get in

1. **Bugs and requests** go into [GitHub Issues](https://github.com/lukislp/status/issues/new/choose). Incident and maintenance issues are different: they are only read from the owner's account (`config.json`, `issues.authors`) and are not a way to report problems to the maintainer. For anything bigger than a small fix, open an issue first so the direction is agreed before you spend time on it.
2. Fork the repository and make your change on a branch.
3. Open a pull request against `main`. The pull request template has the checklist below.
4. `main` is protected: a pull request merges only after the required checks are green, its commits are signed and the branch is up to date. Nobody pushes to `main` directly, not even the maintainer.

## Workflow

- **Conventional Commits** for all commits, in English: `feat: ...`, `fix(check): ...`, `docs: ...`, `ci: ...`, `chore: ...`. Breaking changes use `!` or a `BREAKING CHANGE:` footer.
- **Signed and signed off**: `git commit -S -s` (a verified signature plus the Developer Certificate of Origin sign-off).
- Everything is written in English: code, comments, commits, documentation. The one exception is the German side of the page: its texts are in `statuspage/i18n.py` (every key in both languages, a test checks it) and `name_de` / `description_de` in `config.json`.
- No third-party Python dependencies: the checker and the renderer use the standard library only. The built page makes no third-party requests and sets no cookies.

## Run it locally

Python 3.13, no installation step.

```bash
python -m unittest discover -s tests -t . -v     # unit tests
python -m statuspage validate                     # config.json, incidents/, maintenance/
python -m statuspage build --issues tests/fixtures/issues.json --out _site   # render the site
shellcheck scripts/*.sh                           # the setup script
```

CI runs the same: the `test` and `lint` jobs, CodeQL, and a dependency review on every pull request.

## Components in `config.json`

Every monitored thing is a component in a group:

```json
{
  "id": "portfolio",
  "name": "Portfolio (lktec.org)",
  "description": "One sentence for the page.",
  "checks": [
    { "name": "Home page", "url": "https://lktec.org", "expect_body_contains": "text" }
  ]
}
```

- `name_de` and `description_de` are optional German texts for the German page (`/`); the English page (`/en/`) uses `name` and `description`.
- `id` is unique and lower-case; incidents and the `component:*` issue labels refer to it.
- A check has a `name` and a `url`, and optional expectations: `expect_status`, `expect_body_contains`, `expect_header`, `expect_json`. Per-check overrides: `method`, `interval_minutes`, `timeout_seconds`, `slow_ms`.
- Secrets never go into `config.json`. A header value comes from an environment variable named by `headers_from_env` (the secret itself lives in the repository secrets).
- Run `python -m statuspage validate` after every change; it checks the file. A new or changed check needs a test or a note on how you verified it.

Incidents and planned maintenance are written as GitHub issues by the owner, or as Markdown files from `incidents/_template.md` and `maintenance/_template.md`.

## The `data/` directory

`data/` (`status.json`, `history.json`, `incidents.json`) is the output of the scheduled check. It is committed to `main` by the lktec-bot GitHub App, never by hand and never in a pull request. Do not edit these files; a pull request that touches `data/` will be asked to drop it.

## Definition of done

A change is done when:

1. Unit tests exist and are green; a defect fix adds a regression test that fails without the fix. Exempt are fixes without testable behaviour (documentation, CI configuration, pure styling); say so in the pull request.
2. `python -m statuspage validate` and the site build pass.
3. The README is updated when behaviour or setup changes.
4. The CI checks are green.
