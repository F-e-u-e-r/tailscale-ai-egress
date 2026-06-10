# Configuration Reference

Environment variables and recommended auth-key settings for the Tailscale AI
Egress Connector.

Most users do not need anything here — the interactive `./bootstrap.sh` wizard
covers the common path. These options are for non-interactive runs, CI/automation,
and Advanced Mode. See the [README](../README.md) for the guided setup.

## Environment Variables

```bash
REGION=us # overrides interactive region auto-detection
DRY_RUN=1
CONNECTOR_NAME=AI-Egress-US
CONNECTOR_TAG=tag:ai-egress-us
CONNECTOR_HOSTNAME=ai-egress-us-01 # overrides the hostname keyword prompt
TAG_OWNER=autogroup:admin
MEMBER_SRC=autogroup:member
TAILSCALE_TAILNET=-
TAILSCALE_API_KEY=tskey-api-...
TAILSCALE_API_AUTH=bearer # bearer by default; basic is available for legacy API-key auth
TAILSCALE_API_TIMEOUT=60 # seconds for Tailscale API requests
TAILSCALE_OAUTH_CLIENT_ID=...
TAILSCALE_OAUTH_CLIENT_SECRET=...
TAILSCALE_OAUTH_SCOPES="policy_file devices:core:read devices:posture_attributes:read"
TAILSCALE_AUTHKEY=tskey-auth-... # node auth key, not TAILSCALE_API_KEY
TS_AUTH_KEY=tskey-auth-... # legacy alias for TAILSCALE_AUTHKEY
POLICY_RISK_ACK=1 # CI/automation only; skip Advanced Mode risk confirmation
BOOTSTRAP_RESET_ACK=1 # CI/automation only; allow replacing existing Tailscale settings
ROLLBACK_ACK=1 # skip rollback confirmation in automation
EXIT_NODE_ACK=1 # explicitly acknowledge full-traffic VPS transfer for enable-exit-node.sh
RESTORE_RESET_ACK=1 # acknowledge tailscale up --reset in restore-connector.sh --force-reset
AI_EGRESS_DOMAINS_FILE=/path/to/domains.txt
GENERATED_DIR=/path/to/output
```

`TAILSCALE_API_AUTH` defaults to `bearer`. If bearer auth returns `401`, the policy tool automatically retries Tailscale API-key requests with `basic`, including `apply-plan` revalidation; set `TAILSCALE_API_AUTH=basic` only if you want to force that mode. `TAILSCALE_API_TIMEOUT` defaults to 60 seconds and can be increased for slow networks or unusually large policies.

After a successful bootstrap, the resolved connector region, tag, and hostname
are saved to `generated/connector-identity.env`.

For `restore-connector.sh --force-reset`, the connector tag and hostname come
from one coherent source, in this order:

1. An explicit `CONNECTOR_TAG` and `CONNECTOR_HOSTNAME` pair.
2. An explicit `REGION`, which derives both values.
3. A complete `generated/connector-identity.env`.
4. The current node's Tailscale status, when it has exactly one
   `tag:ai-egress-*` tag and a hostname.

`restore-connector.sh --force-reset` never fills missing fields from a different
source. It stops before `tailscale up --reset` if explicit identity is partial,
the identity file conflicts with the current node, or Tailscale reports multiple
matching connector tags. To intentionally repair a differently configured node,
set both `CONNECTOR_TAG` and `CONNECTOR_HOSTNAME`.

`enable-exit-node.sh` and `disable-exit-node.sh` resolve only the connector tag:
explicit `CONNECTOR_TAG`, then `REGION`, then the identity file, then a unique
matching status tag. Ambiguous or conflicting tags are rejected.

## Recommended Auth Key Settings

When the installer asks for `Tailscale node auth key`, use a pre-authentication auth key from the Tailscale Admin Console Keys page. It should start with `tskey-auth-...`. Do not paste a `tskey-api-...` API token here; API tokens are only for Admin Console policy updates.

Recommended settings for a single VPS connector:

| Setting | Recommended Value | Why |
| --- | --- | --- |
| Description | `ai-egress-<region>-01 bootstrap` | Makes the key easy to identify later |
| Reusable | Off | One VPS should use one one-off key |
| Expiration | 1-7 days | Short-lived is safer; use 90 days only if you cannot bootstrap soon |
| Ephemeral | Off | A persistent Lightsail/VPS connector should remain in the tailnet |
| Tags | On, choose the generated connector tag, such as `tag:ai-egress-us` | Lets the node advertise as the connector tag |
| Pre-approved | On, if shown and your tailnet uses device approval | Avoids a second manual approval step for the server |

If the generated connector tag is not available in the auth key dialog, apply the generated tailnet policy first, then generate the auth key again.
