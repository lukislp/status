# Security Policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's private vulnerability reporting:
https://github.com/lukislp/status/security/advisories/new

Do not open a public issue for security problems. You will get an acknowledgement within a few
days and a fix or mitigation plan before any public disclosure.

## Scope

This repository builds a static status page. The interesting surfaces are:

- **Issue input.** Incidents and maintenance are read from GitHub Issues. Only issues and
  comments authored by the accounts in `config.json` → `issues.authors` are used; all issue
  text is escaped before rendering (no raw HTML, only `http(s)` and relative links). A way to
  get content from someone else onto the page, or to inject markup, is in scope.
- **Workflows.** The scheduled check commits with a write deploy key and an SSH signing key
  (repository secrets). Workflows triggered by issues never check out or run code from the
  issue and only deploy for events sent by the page owner.
- **The page itself.** A strict Content-Security-Policy (`default-src 'none'`), no third-party
  requests, no cookies.

## Supported versions

Only the current state of `main` is deployed and receives fixes.
