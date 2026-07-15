# Advanced Policy Automation

Manual policy setup is the recommended path. The installer can update your Tailscale Admin Console policy automatically only when you explicitly choose Advanced Mode. The prompt always defaults to `N`, even when API or OAuth credentials are already present in the environment.

Review the policy warning carefully: Advanced Mode adds broad internet egress grants and route auto-approvers for the connector tag.

## API Token

```bash
export TAILSCALE_API_KEY="tskey-api-..."
export TAILSCALE_TAILNET="-"
./bootstrap.sh
```

`TAILSCALE_TAILNET=-` tells Tailscale to use the default tailnet for the credential. This API token is only for policy updates; it cannot be used as the VPS node auth key for `tailscale up`.

`TAILSCALE_API_AUTH` defaults to `bearer`. If bearer auth returns `401`, the policy tool automatically retries API-key requests with `basic`, including `apply-plan` revalidation before the final apply call. Set `TAILSCALE_API_AUTH=basic` only if you want to force legacy basic API-key auth.

`TAILSCALE_API_TIMEOUT` defaults to 60 seconds for each Tailscale API request. Increase it on slow links or unusually large policies:

```bash
export TAILSCALE_API_TIMEOUT=120
```

## OAuth Client

```bash
export TAILSCALE_OAUTH_CLIENT_ID="..."
export TAILSCALE_OAUTH_CLIENT_SECRET="..."
export TAILSCALE_TAILNET="-"
./bootstrap.sh
```

The default OAuth scope request is:

```text
policy_file devices:core:read devices:posture_attributes:read
```

If your tailnet requires a different scope string, set:

```bash
export TAILSCALE_OAUTH_SCOPES="policy_file devices:core:read devices:posture_attributes:read"
```

## What Gets Merged

Examples below use `REGION=JP`, which derives `AI-Egress-JP` and `tag:ai-egress-jp`. For other regions, set `REGION=US`, `REGION=SG`, `REGION=TW`, or pass `--connector-name` and `--connector-tag` explicitly.

The policy tool adds or updates:

```text
tagOwners.tag:ai-egress-jp
autoApprovers.routes.0.0.0.0/0
autoApprovers.routes.::/0
grants for autogroup:member -> autogroup:internet
grants for autogroup:member -> tag:ai-egress-jp tcp/udp 53
nodeAttrs app connector config
```

Existing unrelated policy fields are preserved. If a connector with the same name already exists, the connector tag is merged and the configured domains are added without removing domains that were already present in the Admin Console.

The generated grants allow `autogroup:member` to reach `autogroup:internet` on all ports, and the generated auto-approvers allow the connector tag to advertise `0.0.0.0/0` and `::/0`. These settings match the broad egress behavior app connectors need, but they can widen a restrictive tailnet policy. Keep `tagOwners` narrow and review this snippet before applying it to a production tailnet.

When the Tailscale API returns an `ETag` for the fetched policy, the plan records it and `apply-plan` sends it back with `If-Match`. If the policy changed between plan and apply, the tool reports a conflict and asks you to regenerate the plan.

`POLICY_RISK_ACK=1` skips the interactive risk confirmation and is intended only for CI or scripted deployments where the policy change has already been reviewed.

## Recommended Review Flows

Manual snippet, then paste in the Admin Console:

```bash
python3 scripts/policy_tool.py validate \
  --domains-file policy/default-ai-domains.json

python3 scripts/policy_tool.py snippet \
  --domains-file policy/default-ai-domains.json
```

Local preview against a policy downloaded from the Admin Console:

```bash
python3 scripts/policy_tool.py validate --input current.hujson

python3 scripts/policy_tool.py merge \
  --input current.hujson \
  --domains-file policy/default-ai-domains.json \
  --diff
```

Advanced API plan / apply-plan flow:

```bash
TAILSCALE_API_KEY="tskey-api-..." \
python3 scripts/policy_tool.py plan \
  --domains-file policy/default-ai-domains.json \
  --tailnet -

# Inspect the generated bundle before applying.
less generated/policy-plans/plan.<plan-id>/diff.patch
less generated/policy-plans/plan.<plan-id>/manifest.json

python3 scripts/policy_tool.py apply-plan generated/policy-plans/plan.<plan-id>
python3 scripts/policy_tool.py list-plans
```

For scripted checks, add `--report json` to `validate` or `merge`. Plan bundles put the review report into `manifest.json` for valid plans and `report.invalid.json` for failed validation artifacts.

## CI/CD (validate / plan only, no write)

Production automation should stop at `validate` or `plan` — neither writes the policy — unless a human-reviewed deployment step intentionally applies the plan with `apply-plan`:

```bash
python3 scripts/policy_tool.py validate \
  --domains-file policy/default-ai-domains.json \
  --report json

TAILSCALE_API_KEY="$TAILSCALE_API_KEY" \
python3 scripts/policy_tool.py plan \
  --domains-file policy/default-ai-domains.json \
  --tailnet "${TAILSCALE_TAILNET:--}"
```

Do not store API keys in repo files. Use your CI secret manager, keep the token scoped to policy updates, and review the generated `diff.patch` and `manifest.json` before any manual apply job. Non-interactive `apply-plan` and `restore-plan` require all of `--yes`, `POLICY_RISK_ACK=1`, and an explicit plan directory.

## Plan Bundles

This fetches the current policy, merges the connector config, validates it locally and with Tailscale, calls `/acl/preview` when available, and writes an auditable bundle without applying it:

```bash
TAILSCALE_API_KEY="tskey-api-..." \
python3 scripts/policy_tool.py plan \
  --domains-file policy/default-ai-domains.json \
  --tailnet -
```

A valid bundle is written under:

```text
generated/policy-plans/plan.YYYYMMDDTHHMMSSZ-<8-hex-random>/
```

It contains `current.hujson`, `merged.json`, `diff.patch`, `manifest.json`, and optionally `api-preview.json`. The manifest includes the policy tool version, plan id, status, tailnet, connector metadata, original SHA-256 values, `ETag`, summary, and findings. Repeated successful restores keep the original `restored_at` and append later timestamps to `restored_at_history`.

If validation fails, artifacts are written under `generated/policy-plans/failed.<plan-id>/` with no valid `manifest.json`. Inspect `report.invalid.json`, and when available, `current.hujson`, `merged.json`, and `diff.patch`.

The former direct `apply` command has been removed; use `plan` plus `apply-plan`. Running `apply` now prints that pointer and exits non-zero.

## Manual Snippet

To render the policy fragment without touching the API:

```bash
python3 scripts/policy_tool.py validate \
  --domains-file policy/default-ai-domains.json

python3 scripts/policy_tool.py snippet \
  --domains-file policy/default-ai-domains.json
```

To validate a downloaded Admin Console policy before paste or apply:

```bash
python3 scripts/policy_tool.py validate --input current.hujson
```

To preview the exact local merge:

```bash
python3 scripts/policy_tool.py diff \
  --input current.hujson \
  --domains-file policy/default-ai-domains.json
```

## Rollback

For v0.4 plan-based changes, inspect and restore plan bundles:

```bash
python3 scripts/policy_tool.py list-plans
python3 scripts/policy_tool.py restore-plan generated/policy-plans/plan.<plan-id>
```

`restore-plan` restores the `current.hujson` captured before that plan was applied. It verifies the saved SHA-256, validates the policy with Tailscale, fetches a fresh current policy `ETag`, and requires exact `RESTORE <plan-id>` confirmation. A restored plan can be restored again; the original `restored_at` is preserved and later successful restores are appended to `restored_at_history`.

The former direct `apply` command saved timestamped backups (`rollback.sh` still restores any pre-existing ones):

```text
generated/tailnet-policy.backup.YYYYMMDDTHHMMSSZ.hujson
```

To roll back a legacy backup manually, open the Tailscale Admin Console policy page and paste the backup content:

```text
https://login.tailscale.com/admin/acls/file
```

You can also run `./rollback.sh` from the repo for legacy backup files only. With API credentials available, it validates and restores the selected backup through the Tailscale API. Without API credentials, it prints the backup path and Admin Console URL for manual rollback. `rollback.sh` intentionally does not parse plan manifests.

## Comment Preservation

Tailscale policy files can be HuJSON. This tool can read common HuJSON comments and trailing commas, but the merged output is formatted JSON. The plan bundle preserves the original `current.hujson` before any apply.
