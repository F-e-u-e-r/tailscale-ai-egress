<!--
Thanks for contributing! Please keep the change focused and read CONTRIBUTING.md.
Do not include secrets (tskey-auth-..., tskey-api-..., OAuth secrets) anywhere.
-->

## Summary

<!-- What does this change and why? -->

## Type of change

- [ ] Bug fix
- [ ] Documentation
- [ ] Test / CI
- [ ] Feature (in scope per docs/Stability.md)

## How was this tested?

<!-- Commands you ran and what you observed. -->

## Checklist

- [ ] `bash -n` passes for any changed shell script.
- [ ] `shellcheck` passes for any changed shell script.
- [ ] `ruff check scripts tests` and `mypy scripts` pass for Python changes.
- [ ] `python3 -B -m unittest discover -s tests` passes.
- [ ] `python3 -m coverage report --fail-under=70` passes after a coverage run.
- [ ] `./scripts/smoke-test.sh` passes (if behavior changed).
- [ ] `python3 scripts/check_docs_links.py` passes (if docs changed).
- [ ] `python3 scripts/check_readme_parity.py` passes (if `README.md`/`README.zh-HK.md` changed).
- [ ] Updated `CHANGELOG.md` under `## [Unreleased]` (for user-facing changes).
- [ ] Does not break the frozen 1.x surface (CLI entrypoints, plan manifest
      schema major version, `minimal`/`common`/`extended` pack names).
- [ ] No secrets, telemetry, or project-operated servers introduced.
