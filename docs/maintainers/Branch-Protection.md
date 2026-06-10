# Branch Protection

This repository protects `main` with a GitHub repository ruleset. The intended
configuration is defined by
[`scripts/maintainer/apply-github-ruleset.sh`](../../scripts/maintainer/apply-github-ruleset.sh)
so the Console setup can be reviewed and reproduced.

## Intended policy

- Target only `main`.
- Keep the ruleset active with no bypass actors.
- Prevent deletion and force-push updates to `main`.
- Require changes to arrive through a pull request.
- Require all review conversations to be resolved.
- Require the branch to be current with `main` before merging.
- Require these CI checks:
  - `test (3.9)`
  - `test (3.10)`
  - `test (3.11)`
  - `test (3.12)`
- Allow merge, squash, and rebase methods.
- Require zero approving reviews by default. This supports a single-maintainer
  repository while still enforcing the PR and CI gates.
- Automatically delete merged pull-request branches.

The ruleset deliberately does not require signed commits, a linear history,
code-owner review, or a merge queue.

## Before you start

1. Confirm all four checks have run successfully at least once. GitHub only
   offers recently observed checks in the Console selector.
2. Make sure you have repository admin access.
3. Keep another browser tab open on this document while editing the ruleset.
4. Do not enable a rule that is not listed above without first updating this
   document and the reproducible script.

## Configure it in the GitHub Console

1. Open **Settings → Rules → Rulesets**.
2. Select **New ruleset → New branch ruleset**.
3. Set **Ruleset name** to `main-protection`.
4. Set **Enforcement status** to **Active**.
5. Leave **Bypass list** empty.
6. Under **Target branches**, add an include rule for `main` explicitly. Do not
   use the default-branch alias and do not target release tags.
7. Enable **Restrict deletions**.
8. Enable **Require a pull request before merging**.
9. Set required approvals to `0`.
10. Enable **Require conversation resolution before merging**.
11. Leave stale-review dismissal, code-owner review, and last-push approval
    disabled.
12. Allow merge, squash, and rebase merge methods.
13. Enable **Require status checks to pass**.
14. Enable **Require branches to be up to date before merging**.
15. Add the four exact check names listed in [Intended policy](#intended-policy).
    Do not add the `CI /` display prefix or an event suffix.
16. Enable **Block force pushes**.
17. Save the ruleset.
18. Open **Settings → General → Pull Requests**.
19. Enable **Automatically delete head branches**.

## Validate the Console setup

The script uses `gh api`; an SSH key used for Git pushes cannot authenticate
GitHub API requests.

1. Authenticate as a repository administrator:

   ```bash
   gh auth login --hostname github.com --git-protocol ssh --web
   gh auth status
   ```

2. Preview the canonical payload without making API calls:

   ```bash
   ./scripts/maintainer/apply-github-ruleset.sh \
     --repo F-e-u-e-r/tailscale-ai-egress \
     --dry-run
   ```

3. Check the Console configuration:

   ```bash
   ./scripts/maintainer/apply-github-ruleset.sh \
     --repo F-e-u-e-r/tailscale-ai-egress \
     --check
   ```

4. A successful check prints:

   ```text
   [OK] GitHub ruleset and merged-branch cleanup match the desired configuration.
   ```

If `--check` reports drift, compare the named field with the Console steps
above. Running the script without `--check` will make the managed ruleset match
the documented configuration.

## Apply or repair with the script

```bash
./scripts/maintainer/apply-github-ruleset.sh \
  --repo F-e-u-e-r/tailscale-ai-egress
```

The command is idempotent:

- It creates `main-protection` when it does not exist.
- It updates the exact same ruleset when it already exists.
- It enables automatic deletion of merged pull-request branches.
- It reads credentials from `gh`; it never accepts or prints a token.

For a repository with multiple maintainers, require one approval:

```bash
./scripts/maintainer/apply-github-ruleset.sh \
  --repo F-e-u-e-r/tailscale-ai-egress \
  --required-approvals 1
```

Use the same `--required-approvals` value with `--check`.

## Verification after activation

1. Open a small documentation PR.
2. Confirm direct pushes to `main` are rejected.
3. Confirm all four CI matrix jobs are required.
4. Confirm unresolved review conversations block merging.
5. Merge the PR and confirm its head branch is deleted.
6. Run the script with `--check` again.

## Recovery

If the ruleset blocks an urgent repair:

1. Open **Settings → Rules → Rulesets → main-protection**.
2. Change enforcement to **Disabled** rather than deleting the ruleset.
3. Make the minimum repair through a reviewed PR where possible.
4. Restore **Active** enforcement.
5. Run the script with `--check`.

Deleting or renaming the ruleset makes the scripted state harder to audit and
may cause the next apply operation to create a second ruleset.
