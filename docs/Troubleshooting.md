# Troubleshooting

## The Policy Editor Rejects The Connector Name

The examples below use `REGION=JP`. If you set another region, replace the connector name and tag with the values derived from your `REGION`; if you chose a hostname keyword, replace the hostname with that single-device hostname.

Use a simple hyphenated name:

```text
AI-Egress-JP
```

Avoid spaces if the policy editor rejects them.

## The AI Domain Does Not Route Through Tailscale

Run the client route checker from the Mac or Linux client:

```bash
./check-client-routes.sh
```

It checks all IPv4 A records for each configured AI domain. If some IPs route
through Tailscale and others do not, it reports `[WARN]` because DNS discovery
or route advertisement may still be settling.

Manual route checks are still useful:

```bash
dig +short chatgpt.com A
route -n get <one-of-the-returned-IPs>  # macOS
ip route get <one-of-the-returned-IPs>
```

On macOS, Tailscale interface names are dynamic (`utun3`, `utun8`, and so on),
and App Store vs standalone builds can expose different route-table details. On
Linux, userspace networking mode may not expose a `tailscale0` interface, so the
route table can be misleading.

If it still uses the local default route:

- Confirm the tailnet policy was saved.
- Confirm the connector node is online.
- Confirm the node has the expected tag, such as `tag:ai-egress-jp`.
- Wait 1-2 minutes for DNS discovery and route advertisement, then rerun `./check-client-routes.sh`.
- Restart Tailscale on the client if route state looks stale.

## Public IP Still Shows Local ISP

That is expected for ordinary traffic. App Connector mode routes only matching app domains. Use `route -n get` or browser testing against the target domain instead of a generic IP check.

If you intentionally selected an exit node on the client, public IP checks should show the exit node's egress IP instead. In that mode, all non-Tailscale traffic is using the selected exit node, not only AI domains.

## Baseline Traffic Uses Tailscale Unexpectedly

Run:

```bash
./check-client-routes.sh
```

The baseline domain defaults to `ipinfo.io`. In connector-only mode, it should stay local. If it uses Tailscale:

- Confirm the client has not selected an exit node.
- Check for broader subnet/default routes on the client.
- Re-run with a site you noticed behaving oddly:

```bash
./check-client-routes.sh --baseline-domain affected-site.example
```

If the affected site shares a CDN IP with a configured AI domain, see the shared CDN section below.

## Shared CDN IPs Route More Than Expected

App Connectors are selected by domain, but the actual route is installed for the resolved IP address. If a target domain uses a shared CDN IP, some unrelated traffic to that same IP may also route through the connector.

To diagnose a suspected case, run the baseline check against the affected site:

```bash
./check-client-routes.sh --baseline-domain affected-site.example
```

If it routes through Tailscale while no exit node is selected, compare its resolved IPs with the AI domains being routed. Mitigate by using a custom `--domains-file` that removes broad provider/CDN domains you do not need. This is a route granularity limitation of IP-based route installation, not a VPS failure.

## Exit Node Is Not Visible

On the connector host:

```bash
./diagnose.sh
./enable-exit-node.sh --dry-run
```

If `diagnose.sh` says exit-node advertising is unknown or disabled:

- Confirm you ran `./enable-exit-node.sh` on an already-bootstrapped Linux connector host.
- Confirm IP forwarding is enabled.
- Open the Tailscale Admin Console Machines page and approve the advertised exit node if your policy requires approval.
- Wait a minute for client state to update, then reopen the client's exit-node menu.

Cloud VPS exit-node fallback is full-traffic mode. Use it sparingly unless your VPS transfer allowance is sized for it.

## Restore Back To Connector Mode

Use:

```bash
./disable-exit-node.sh
./restore-connector.sh
```

The default restore path preserves local Tailscale preferences. If a troubleshooting command used `tailscale up --reset` and removed hostname/tag/app-connector flags, use the explicit repair path:

```bash
./restore-connector.sh --force-reset
```

Read the warning carefully: `--force-reset` can clear local preferences such as accepted routes, DNS settings, and manually added flags.

## IPv6 Is Weird

Some low-cost VPS plans do not provide stable IPv6 egress. The bootstrap enables IPv6 forwarding when available, but you should test:

```bash
curl -6 https://ifconfig.co/ip
sysctl net.ipv6.conf.all.forwarding
dig +short chatgpt.com AAAA          # does the domain even publish IPv6?
route -n get -inet6 <ip6>            # macOS; on Linux: ip -6 route get <ip6>
```

`check-client-routes.sh` inspects IPv6 (AAAA) routes as an **advisory** check (`*-ipv6` check ids) when a domain publishes them, and skips cleanly when it does not — IPv6 findings never fail the run, so IPv4 remains the pass/fail signal until you have confirmed provider IPv6 support.

## `tailscale up` Complains About Non-Default Flags

Tailscale sometimes requires all previously used non-default flags to be provided again. Prefer the helper, which reuses the bootstrapped identity when available:

```bash
./restore-connector.sh --force-reset
```

If you must run `tailscale up --reset` manually, use the actual hostname and tag
from `generated/connector-identity.env`, the bootstrap output, or Tailscale
status:

```bash
sudo tailscale up \
  --reset \
  --hostname=ai-egress-us-01 \
  --advertise-connector \
  --advertise-tags=tag:ai-egress-us
```

For normal connector and exit-node mode switching, prefer `./enable-exit-node.sh`, `./disable-exit-node.sh`, and `./restore-connector.sh`. Use the full `tailscale up --reset` command only when you intentionally want to repair the local Tailscale preferences back to bootstrap-style connector flags.

## Advanced Policy Automation Fails

The installer should fall back to manual mode. To debug:

```bash
TAILSCALE_API_KEY="tskey-api-..." \
python3 scripts/policy_tool.py plan \
  --domains-file policy/default-ai-domains.json \
  --tailnet -
```

Common API failure cases:

- `401` or `403`: check whether the API key/OAuth client has policy-file access, and confirm `TAILSCALE_TAILNET=-` or the explicit tailnet name is correct.
- `412`: the Admin Console policy changed between `plan` and `apply-plan`. Regenerate the plan against the latest policy before re-running `apply-plan`.
- Validation errors: inspect `generated/policy-plans/failed.<plan-id>/report.invalid.json` and any generated `merged.json` or `diff.patch`.
- Manifest update errors after a successful `apply-plan` or `restore`: inspect the current Admin Console policy before retrying. The tool reports that the API write succeeded, but it could not update `manifest.json`.
- Network timeouts: raise `TAILSCALE_API_TIMEOUT`, for example `export TAILSCALE_API_TIMEOUT=120`.

If validation fails, inspect:

```text
generated/policy-plans/failed.<plan-id>/report.invalid.json
generated/policy-plans/failed.<plan-id>/merged.json
generated/policy-plans/failed.<plan-id>/diff.patch
```

Rollback options are documented in the README and `docs/Tailscale-API-mode.md`. In short, use `python3 scripts/policy_tool.py list-plans` and `python3 scripts/policy_tool.py restore-plan <plan-dir>` for v0.4 plan restores. Use `./rollback.sh --list` only for legacy `tailnet-policy.backup.*.hujson` files.
