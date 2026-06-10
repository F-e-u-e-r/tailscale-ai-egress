# Release Checklist

Steps to cut a `vX.Y.Z` release. Most checks are automated by CI and the
`scripts/smoke-test.sh` sequence; this list is the human-facing gate.

## 1. Clean checkout

Work from a clean tree so nothing local leaks into the release.

```bash
git status --porcelain   # expect no output
git clean -ndx           # review what is untracked/ignored
```

## 2. Version bump

- [ ] Update [`VERSION`](../VERSION) and `__version__` in both
      [`scripts/policy_tool.py`](../scripts/policy_tool.py) and
      [`scripts/health_check.py`](../scripts/health_check.py) to the new version
      (they must match; unit tests enforce it).
- [ ] Move the `## [Unreleased]` notes in [`CHANGELOG.md`](../CHANGELOG.md) into a
      new `## [X.Y.Z] - YYYY-MM-DD` section and update the compare/tag links.

## 3. Static checks

```bash
# Shell syntax
for f in bootstrap.sh check-client-routes.sh diagnose.sh disable-exit-node.sh \
         enable-exit-node.sh failover-exit-node.sh monitor-connectors.sh \
         install.sh restore-connector.sh rollback.sh \
         scripts/smoke-test.sh scripts/package.sh scripts/validation-e2e.sh \
         scripts/maintainer/apply-github-ruleset.sh; do
  bash -n "$f"
done

# Shell lint
shellcheck bootstrap.sh check-client-routes.sh diagnose.sh disable-exit-node.sh \
  enable-exit-node.sh failover-exit-node.sh monitor-connectors.sh \
         install.sh restore-connector.sh rollback.sh \
  scripts/smoke-test.sh scripts/package.sh scripts/validation-e2e.sh \
  scripts/maintainer/apply-github-ruleset.sh

# Python lint/type checks
ruff check scripts tests
mypy scripts
```

## 4. Unit tests

```bash
python3 -B -m unittest discover -s tests
python3 -m coverage run -m unittest discover -s tests
python3 -m coverage report --fail-under=55
```

## 5. Credential-free smoke test

Runs the local-only and dry-run paths (versions, `domains`, `snippet`,
`validate`, `merge`, `diff`, `bootstrap.sh --dry-run`, `--help`/`--version` for
every entrypoint):

```bash
./scripts/smoke-test.sh
```

## 6. Dry-run bootstrap

```bash
./bootstrap.sh --dry-run --domain-pack common
```

Confirm the privileged commands are printed, no live diagnostics run, and no
Advanced Mode prompt appears without credentials.

## 7. Policy plan / apply-plan dry run

With a **read-capable** API or OAuth credential against a scratch tailnet:

```bash
# Generate an auditable bundle (fetches + validates; does not apply).
python3 scripts/policy_tool.py plan --tailnet - \
  --domains-file policy/default-ai-domains.json

# Inspect the bundle, do NOT apply during the checklist.
ls generated/policy-plans/plan.*/
sed -n '1,40p' generated/policy-plans/plan.*/diff.patch

# Optional: preview the merged policy without writing anything.
python3 scripts/policy_tool.py apply --tailnet - --dry-run --diff \
  --domains-file policy/default-ai-domains.json
```

`apply-plan` itself writes the policy, so it is exercised by the mocked unit
tests rather than during the checklist. Only run it against a real tailnet you
intend to change.

## 8. Diagnostics smoke tests

The fake-command diagnostics paths are covered by the unit tests
(`tests/test_shell_scripts.py`). On a real host you can additionally run:

```bash
./diagnose.sh --json
./check-client-routes.sh --json
```

## 9. Docs link check

```bash
python3 scripts/check_docs_links.py
```

## 10. Packaging check

```bash
./scripts/package.sh --check        # build + verify contents and SHA256SUMS
ls dist/
cat dist/SHA256SUMS
```

## 11. Tag and publish

```bash
git tag -a vX.Y.Z -m "vX.Y.Z"
git push origin vX.Y.Z
```

Pushing a `v*` tag triggers `.github/workflows/release.yml`, which builds the
`tar.gz` + `zip` + `SHA256SUMS`, extracts the matching `CHANGELOG.md` section as
release notes, and creates the GitHub release. After it runs:

- [ ] Verify the release has the three artifacts plus `SHA256SUMS`.
- [ ] Verify the release notes match the changelog section.
- [ ] Download an artifact and re-check its checksum against `SHA256SUMS`.

## 12. Post-release

- [ ] Confirm the `## [Unreleased]` section is back to empty for the next cycle.
- [ ] Update [Validation-Matrix.md](Validation-Matrix.md) with any new results.
