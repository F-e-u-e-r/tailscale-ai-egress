# Support

This is a community, best-effort open-source project. There is no commercial
support, SLA, or hosted service behind it. The maintainers help when they can.

## Before asking for help

1. Re-run the diagnostics and capture the JSON output:
   ```bash
   ./diagnose.sh --json            # on the VPS connector host
   ./check-client-routes.sh --json # on the client device
   ```
2. Read [docs/Troubleshooting.md](docs/Troubleshooting.md). Most route-coverage,
   DNS, and userspace-networking questions are answered there.
3. Confirm your setup is in scope (see "Supported configurations" below).

## Where to ask

- **Questions / how-to / "is this expected?":** open a
  [GitHub Discussion or issue](https://github.com/F-e-u-e-r/tailscale-ai-egress/issues)
  using the question template.
- **Bug reports:** open a GitHub issue using the bug template. Include the
  `--json` diagnostics, your OS and Tailscale version, the provider, and the
  exact command you ran.
- **Feature ideas:** open an issue using the feature template. Note that the
  1.x scope is intentionally frozen (see [docs/Stability.md](docs/Stability.md)).
- **Security vulnerabilities:** do not open a public issue. Follow
  [SECURITY.md](SECURITY.md).

## Supported configurations

These are the configurations targeted for 1.0. Others may work but are
best-effort. See [docs/Validation-Matrix.md](docs/Validation-Matrix.md) for the
full matrix and the current manual validation status.

- **Server OS:** Ubuntu/Debian first. AWS Lightsail (Ubuntu 24.04 LTS),
  WebARENA (supported Ubuntu/Debian image), and a generic Ubuntu/Debian VPS with
  a public IPv4 are the documented provider paths. Fedora/CentOS/Alpine are
  best-effort unless already verified in the validation matrix.
- **Client OS:** macOS (App Store or standalone Tailscale) and Linux (normal TUN
  mode, plus the userspace-networking warning path).
- **Modes:** App Connector only, same-region connector failover, exit-node
  fallback (enabled/disabled), and Advanced policy plan/apply/restore.

## What is out of scope

- Tailscale account, billing, or platform problems — contact
  [Tailscale support](https://tailscale.com/contact/support).
- VPS provisioning, networking, or billing — contact your VPS provider.
- AI service provider terms, availability, or account issues — contact that
  provider.
- Anything in the "out of scope" list of [CONTRIBUTING.md](CONTRIBUTING.md):
  GUIs, telemetry, extra proxy protocols, automatic VPS purchasing.

## Response expectations

Issues and pull requests are handled on a best-effort basis with no guaranteed
response time. Clear, reproducible reports with diagnostics attached are the
fastest to resolve.
