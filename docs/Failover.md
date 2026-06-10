# Multi-Machine Failover

This guide focuses on AI egress. The examples below keep AI-related domains on a predictable Tailscale App Connector path while normal traffic stays local or on the user's chosen exit node.

Use this guide when you want two connector machines in the same egress region, usually across different providers, so a second machine can take over new matching connections if the first machine goes offline. For the single-machine VPS flow, start with [AWS Lightsail Notes](AWS-Lightsail.md), [WebARENA Notes](WebARENA.md), or [Generic VPS Notes](Generic-VPS.md).

Official Tailscale references:

- [App connectors](https://tailscale.com/kb/1281/app-connectors)
- [High availability](https://tailscale.com/kb/1115/high-availability)

## Intended Topology

The intended pool is same-region, multi-provider failover:

| Provider | Hostname | Connector Tag |
| --- | --- | --- |
| AWS Lightsail | `ai-egress-jp-aws-01` | `tag:ai-egress-jp` |
| WebARENA | `ai-egress-jp-web-01` | `tag:ai-egress-jp` |

Both machines use the same connector tag because they serve the same egress region and the same AI domain set. If you intentionally use cross-region egress, use different tags such as `tag:ai-egress-jp` and `tag:ai-egress-us` to avoid unexpected latency or region switching.

AWS Lightsail and WebARENA may differ in IPv6 support, firewall defaults, and bandwidth quota. Bring each provider up as a working single connector before pairing them for failover. This is connector failover, not exit-node failover; normal traffic should stay local unless a client deliberately selects an exit node.

## Bootstrap The Two Machines

On the AWS Lightsail VPS:

```bash
REGION=JP CONNECTOR_HOSTNAME=ai-egress-jp-aws-01 ./bootstrap.sh
```

On the WebARENA VPS:

```bash
REGION=JP CONNECTOR_HOSTNAME=ai-egress-jp-web-01 ./bootstrap.sh
```

Use a Tailscale node auth key (`tskey-auth-...`) tagged or allowed for `tag:ai-egress-jp` on both machines. If `tag:ai-egress-jp` is not available in the auth key dialog, apply the generated tailnet policy first, then generate the key again. Do not paste a `tskey-api-...` token at the node auth prompt; API tokens are only for Admin Console policy automation.

## Second-Machine Policy Prompt

If the first machine already applied a policy containing `tagOwners`, `autoApprovers`, and `nodeAttrs` for `tag:ai-egress-jp`, answer `n` when the second machine asks whether the installer should update your Tailscale policy automatically. The policy already contains the shared connector tag, so this avoids extra API calls and plan bundles.

If you are unsure, answering `y` is safe. `scripts/policy_tool.py plan` merges policy idempotently, so it should preserve existing entries and create a reviewable plan before any apply.

Optional visibility check before the second bootstrap:

```bash
tailscale status | grep -E 'tag:ai-egress-jp|ai-egress-jp-' || \
  echo "No ai-egress-jp connector visible from this client yet"
```

If you prefer a UI check instead, use the Tailscale Admin Console Machines page for device/tag visibility and the policy page for connector policy details.

## Verification

Verify that both machines are visible:

```bash
tailscale status | grep ai-egress-jp
```

Verify client routes for configured AI domains:

```bash
./check-client-routes.sh
```

The checker tests every IPv4 A record and reports `[OK]`, `[WARN]`, or `[FAIL]`. A short `[WARN]` period can be normal while App Connector DNS discovery and route advertisement settle.

To test failover, stop `tailscaled` on one connector, wait for Tailscale clients to detect the route change, and recheck the route or browser behavior from a client. Existing connections may break when a connector disappears. New connections should move to another online connector after Tailscale detects the route change.

## Health Checks

At minimum, monitor that each connector is online in Tailscale and that sample AI domains still resolve to a Tailscale route from a client:

```bash
tailscale status | grep ai-egress-jp
./diagnose.sh
```

For lightweight server-side monitoring, run diagnostics from cron and send the output to your normal alerting or log collection path:

```cron
*/5 * * * * cd /path/to/tailscale-ai-egress && ./diagnose.sh >/var/log/tailscale-ai-egress-diagnose.log 2>&1
```

For production use, pair this with provider-level monitoring for VPS reachability, bandwidth quota, and disk space. Tailscale failover helps with connector availability, but it does not replace VPS health or billing/quota alerts.

## Taking One Machine Out

| Action | Effect |
| --- | --- |
| `sudo systemctl stop tailscaled` | Temporarily takes a systemd-based connector offline; it may return after service restart or reboot |
| `sudo rc-service tailscale stop` | Alpine/OpenRC alternative for temporarily taking the connector offline |
| `sudo tailscale logout` | Disconnects the node from the tailnet; the machine record may remain in the Admin Console |
| Remove machine in Admin Console | Removes the device record from the tailnet |
