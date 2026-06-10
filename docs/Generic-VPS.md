# Generic VPS Notes

Use this guide for providers that do not have a dedicated document in this repo.

## Minimum Expectations

Choose a VPS with:

- Linux with `systemd` or OpenRC support.
- Public IPv4.
- Working outbound HTTPS.
- A package manager supported by `bootstrap.sh`: `apt-get`, `dnf`, `yum`, or `apk`.
- Enough bandwidth for your intended use.

App Connector mode is selective and usually light on transfer. Exit-node fallback is full-traffic mode and can use far more VPS data.

## Provider Pitfalls

- Avoid IPv6-only plans unless you are prepared to debug IPv4 reachability separately.
- Avoid NAT-only plans where the instance does not have stable outbound IPv4.
- Check whether the provider blocks or rate-limits VPN-like traffic.
- Check whether firewall rules allow SSH and outbound HTTPS.
- Check the public IP reputation if the use case depends on service availability from that IP.
- Prefer a region close to the AI service region you want to test, not merely close to your home device.

## Setup

```bash
git clone https://github.com/F-e-u-e-r/tailscale-ai-egress.git
cd tailscale-ai-egress
./bootstrap.sh
```

If `REGION` is unset, interactive bootstrap tries to detect the VPS country code
from its public IP and lets you confirm or override it. Set `REGION` and
`CONNECTOR_HOSTNAME` explicitly when you want a deterministic scripted setup:

```bash
REGION=SG CONNECTOR_HOSTNAME=ai-egress-sg-01 ./bootstrap.sh
```

In non-interactive mode, set `REGION` when public-IP country detection is not
available. The script fails instead of silently choosing a different region.

## Validation

On the VPS:

```bash
./diagnose.sh
```

On a client:

```bash
./check-client-routes.sh
```

If you intentionally enable exit-node fallback, repeat the transfer warning to anyone using that client: every selected-client internet request can traverse the VPS.
