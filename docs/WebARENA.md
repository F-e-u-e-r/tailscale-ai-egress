# WebARENA Notes

WebARENA Indigo can be a low-cost Japan connector provider, but treat provider details as something to verify during purchase. This project does not buy or provision the VPS for you.

## Recommended Shape

Use a normal Linux VPS plan with public IPv4:

```text
OS: Ubuntu LTS or another supported Linux distribution
Network: public IPv4 required
Region: Japan
Use: App Connector first, exit-node fallback only when intentional
```

Do not choose an IPv6-only shape for this project. Many AI services and CDNs still rely on IPv4 A records, and the client checker intentionally validates IPv4 routing. If WebARENA changes its plan mix, pick a plan that clearly includes public IPv4.

## Before Bootstrap

- Wait until the instance is fully created and SSH works reliably.
- Confirm the OS package manager is usable.
- Confirm outbound HTTPS works:

```bash
curl -4 https://ifconfig.co/ip
curl -fsS https://tailscale.com/install.sh >/dev/null
```

- Check provider firewall/security-group rules allow SSH from your admin IP.
- If IPv6 is advertised, validate it separately; IPv6 support is useful but not required for the default IPv4 route checks.

## Bootstrap

```bash
git clone https://github.com/F-e-u-e-r/tailscale-ai-egress.git
cd tailscale-ai-egress
REGION=JP CONNECTOR_HOSTNAME=ai-egress-jp-web-01 ./bootstrap.sh
```

Use a Tailscale node auth key tagged or allowed for `tag:ai-egress-jp`. Do not paste a `tskey-api-...` token at the node auth prompt.

## Post-Create Validation

On the WebARENA VPS:

```bash
./diagnose.sh
```

On a client in the same tailnet:

```bash
./check-client-routes.sh
```

If the client checker warns immediately after setup, wait 1-2 minutes for App Connector DNS discovery and route propagation.

## Cost And Transfer Caution

App Connector mode sends only selected AI-domain traffic through the VPS. Exit-node fallback sends all selected-client internet traffic through the VPS and can consume much more transfer. Do not use a WebARENA cloud VPS as an always-on full exit node unless you have checked the current plan allowance and overage behavior.

## Failover Pairing

WebARENA works best in this repo as a second same-region connector paired with another provider, such as AWS Lightsail, under the same connector tag. See [Failover.md](Failover.md).
