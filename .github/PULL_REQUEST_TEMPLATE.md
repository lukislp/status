## What and why

<!-- One or two sentences: what changes, and why. Link the issue if there is one. -->

## Checklist

- [ ] Conventional Commit title (`fix:`, `feat:`, `ci:`, `docs:`, ...), see CONTRIBUTING.md
- [ ] Tests, or a manual verification note (`python -m unittest discover -s tests -t .`)
- [ ] A defect fix has a regression test that fails without the fix, or the reason it is exempt
- [ ] `python -m statuspage validate` passes (config, incidents, maintenance)
- [ ] Documentation updated (README, `config.json` comments in the PR, templates)
- [ ] No secrets, tokens or personal data in code, tests or logs
- [ ] No hand-edited files under `data/` (written by automation only)
- [ ] Commits signed and signed off (`git commit -S -s`, Developer Certificate of Origin)
