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

## Choosing The Primary (Oldest-First)

Tailscale's default connector failover is **oldest-first**: connector selection follows the order in which connectors joined the tailnet, the oldest connector is the primary, and failover proceeds oldest-first. This is the default behavior on all plans (see [High availability](https://tailscale.com/kb/1115/high-availability)). The per-client pseudorandom, "sticky" selection described for *regional routing* is a separate Premium/Enterprise feature and does not apply to this default.

So you do not need policy edits to get a deterministic primary/fallback pair — just control the join order. To make WebARENA the primary and AWS the fallback:

1. If needed, remove or re-authenticate both connector machines so their join order is clean.
2. Bring up **WebARENA first** and let it come online.
3. Bring up **AWS second**.
4. Keep both online with the same connector tag and the same AI domain set.

Tailscale then treats WebARENA (the oldest) as primary and fails over to AWS when WebARENA goes offline. With `tailscale down`, failover takes up to ~15 seconds; a network partition can take longer. This project intentionally does not reassign connectors via the policy API — native oldest-first ordering is enough for a primary/fallback pair.

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

### Connector Monitor (`monitor-connectors.sh`)

`monitor-connectors.sh` is a read-only helper that checks both connectors in a primary/fallback pair. It reports each connector's online state and tailnet reachability (via `tailscale ping`), and — only when a `TAILSCALE_API_KEY` is configured — the `device.created` ordering, so you can confirm the intended primary is the oldest. Without an API token it prints `ordering=unavailable` and still runs the health checks; ordering uses an API key (`TAILSCALE_API_KEY`) only (OAuth is not used for this optional check). It also reports `routes_serving` — which connector is advertising app-connector routes — and degrades when neither is; set `REQUIRE_ROUTES=0` if your client cannot observe connector routes. This route check is best-effort: it confirms a non-default route is advertised, not that specific AI app-domain routes resolve. It never switches anything. Both the monitor and the controller require the local Tailscale backend to be `Running`; if it is `Stopped`, `NeedsLogin`, or `Starting`, the monitor reports degraded and the controller makes no switch (fails closed).

```bash
PRIMARY_CONNECTOR=ai-egress-jp-web-01 FALLBACK_CONNECTOR=ai-egress-jp-aws-01 \
  ./monitor-connectors.sh --once
```

Exit status is `0` when both connectors are online and reachable and `1` when degraded, so it suits cron alerting:

```cron
*/5 * * * * cd /path/to/tailscale-ai-egress && ./monitor-connectors.sh --once >>/var/log/tailscale-ai-egress-monitor.log 2>&1
```

## Exit-Node Failover (Full Traffic)

Connector failover above only covers the selected AI domains. If you also route *all* other traffic through an exit node, note that Tailscale does not provide priority-ordered exit-node failover (only `--exit-node=auto:any`, which selects by latency rather than a fixed primary/fallback order). `failover-exit-node.sh` adds a client-side primary/fallback exit-node controller for macOS and Linux.

It probes a primary and fallback exit node and, when run with `--apply`, switches the local `tailscale set --exit-node` to the fallback when the primary fails its tailnet ping, then switches back when the primary recovers (unless `RESTORE_PRIMARY=0`). It is observe-first: without `--apply` it only reports the proposed action, and it only ever switches to a node that passed its ping in the same cycle.

By default, the controller manages an *already selected* exit node and does not
turn one on by itself. Select your primary once first, or explicitly let the
controller make the initial selection with `--ensure-primary`:

```bash
cp examples/failover.env.example generated/failover.env   # then edit PRIMARY/FALLBACK exit nodes

# Option A: select the primary yourself once, then run the controller.
tailscale set --exit-node=<your-primary>
./failover-exit-node.sh --watch --apply

# Option B: let the controller select the primary when none is set.
./failover-exit-node.sh --watch --apply --ensure-primary

# Observe first (no change) at any time:
./failover-exit-node.sh --once
```

iOS and Android cannot run this watcher (switch the exit node in the app); Windows is not yet supported. See [Configuration](Configuration.md) for the full list of `failover.env` settings, and the `docs/examples/` directory for ready-made systemd, launchd, and cron units.

### Post-Switch Diagnostics With `peer-metrics`

The controller ships **no built-in** post-switch metrics collection: under the 1.x
rule its *own code* never puts a metrics fetch on the failover decision or exit path,
so by default a slow or failing metric cannot stall or fail a switch.

You can still log the *new* exit node's read-only metrics after each switch by
composing the existing `FAILOVER_NOTIFY_CMD` hook with the `peer-metrics` subcommand.
This needs **no controller code change** and **cannot change the failover decision or
the already-recorded switch/cooldown** — the switch is recorded before the hook runs,
and the hook's exit status is ignored. It does, however, **extend the controller's
process path while it runs**: the hook is *synchronous*, so keep it fast (a slow
command stalls the watch loop), and a `SIGTERM` reaching the controller *while the
hook is running* makes it exit `143` rather than `0`. That is your informed opt-in,
exactly as for any `FAILOVER_NOTIFY_CMD`. The hook receives `FAILOVER_EVENT`
(`switched`|`failed`), `FAILOVER_ROLE`, `FAILOVER_LABEL`, and `FAILOVER_REASON` in its
environment; gate on `switched` so it does not run on a failed attempt:

```bash
# Log the new exit node's tx/rx, connection path, latency, and handshake age after
# each successful switch (adjust the install path). `peer-metrics --ping` is
# read-only and always exits 0; see docs/design/metrics-collection.md.
export FAILOVER_NOTIFY_CMD='[ "$FAILOVER_EVENT" = switched ] && \
  python3 /opt/tailscale-ai-egress/scripts/health_check.py \
    peer-metrics --node "$FAILOVER_LABEL" --ping | logger -t ai-egress-failover'
./failover-exit-node.sh --watch --apply
```

## Taking One Machine Out

| Action | Effect |
| --- | --- |
| `sudo systemctl stop tailscaled` | Temporarily takes a systemd-based connector offline; it may return after service restart or reboot |
| `sudo rc-service tailscale stop` | Alpine/OpenRC alternative for temporarily taking the connector offline |
| `sudo tailscale logout` | Disconnects the node from the tailnet; the machine record may remain in the Admin Console |
| Remove machine in Admin Console | Removes the device record from the tailnet |
