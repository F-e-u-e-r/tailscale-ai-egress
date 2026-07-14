# AWS Lightsail Notes

This project works well with a small Ubuntu Lightsail instance in the same region you want for egress, such as Tokyo.

## Suggested First VPS

```text
Platform: Linux/Unix
OS: Ubuntu 24.04 LTS
Region: Tokyo
Networking: Public IPv4 enabled
```

## Bootstrap

SSH into the VPS and run:

```bash
git clone https://github.com/F-e-u-e-r/tailscale-ai-egress.git
cd tailscale-ai-egress
REGION=JP ./bootstrap.sh
```

`REGION=JP` derives the connector name, connector tag, and default hostname region. In interactive mode, press Enter at the hostname keyword prompt to keep the default hostname:

```text
CONNECTOR_NAME=AI-Egress-JP
CONNECTOR_TAG=tag:ai-egress-jp
CONNECTOR_HOSTNAME=ai-egress-jp-01
```

For other regions, set `REGION=US`, `REGION=SG`, or another short region code. The optional hostname keyword changes only the device hostname, not the connector name or tag.

Use a Tailscale node auth key (`tskey-auth-...`) tagged or allowed for the derived tag. Do not paste a Tailscale API key (`tskey-api-...`) at the node auth prompt. For Tokyo/JP, the tag is:

```text
tag:ai-egress-jp
```

## Recommended Auth Key Settings

In the Tailscale Admin Console, generate a node auth key with these settings:

| Setting | Recommended Value |
| --- | --- |
| Description | `ai-egress-jp-01 bootstrap` |
| Reusable | Off |
| Expiration | 1-7 days, or 90 days only if needed |
| Ephemeral | Off |
| Tags | On, select `tag:ai-egress-jp` |
| Pre-approved | On, if your tailnet shows this option |

The generated key should start with `tskey-auth-...`. A `tskey-api-...` token is for policy automation and will not work at the VPS node auth prompt.

## Bandwidth

App Connector mode is usually cheaper than using the VPS as a full-time exit node because only selected domain traffic uses the VPS egress path.

If you enable exit-node fallback with `./enable-exit-node.sh`, every selected-client internet request can traverse the Lightsail instance. Check the current transfer allowance before using a cloud VPS as an always-on full exit node.

## Confirm Egress

On the VPS:

```bash
./diagnose.sh
./diagnose.sh --json
```

On a macOS client:

```bash
./check-client-routes.sh
```

On a Linux client:

```bash
./check-client-routes.sh
```

The client checker tests every IPv4 A record for the selected AI domains and
prints `[OK]`, `[WARN]`, or `[FAIL]` for each route (it also inspects IPv6/AAAA
advisorily when present; those findings never fail the run). App Connector route
discovery can take 1-2 minutes after setup; rerun the client check if routes are
not visible immediately.
