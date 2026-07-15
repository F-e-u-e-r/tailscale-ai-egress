# Validation Matrix

The combinations 1.x targets, and how each is verified. This is a living
document: fill in the "Last validated" cells when you run a real check, and
attach the captured artifacts.

## Legend

- **Automated** — exercised in CI by unit tests (fake commands / mocked API).
- **Manual** — requires real hardware, a real client, or a live tailnet.
- **Pending** — not yet validated for this release; record the date when done.

For every **Manual** run, capture and attach:

1. `./diagnose.sh --json` (VPS connector host)
2. `./check-client-routes.sh --json` (client device)
3. Provider notes (region, plan, anything unusual)
4. Tailscale version (`tailscale version`)
5. OS version (`cat /etc/os-release` or `sw_vers`)
6. Any known limitations observed

You can collect the two JSON artifacts with:

```bash
VPS_SSH=user@connector-host \
CLIENT_SSH=user@client-device \
VPS_REPO_DIR=/path/to/tailscale-ai-egress \
CLIENT_REPO_DIR=/path/to/tailscale-ai-egress \
./scripts/validation-e2e.sh
```

The script writes evidence under `generated/validation/<timestamp>/`. If either
host is the current machine, leave that `*_SSH` variable unset or set it to
`local`.

A ready-to-fill template is at the bottom of this page.

## Providers (server OS)

| Provider | Image | Coverage | Last validated | Notes |
| --- | --- | --- | --- | --- |
| AWS Lightsail | Ubuntu 24.04 LTS | Manual | _pending_ | See [AWS-Lightsail.md](AWS-Lightsail.md) |
| WebARENA | Supported Ubuntu/Debian image | Manual | _pending_ | See [WebARENA.md](WebARENA.md) |
| Generic VPS | Ubuntu/Debian with public IPv4 | Manual | _pending_ | See [Generic-VPS.md](Generic-VPS.md) |

Other Linux distributions (Fedora/CentOS/Alpine) are best-effort and not part of
the 1.0 validated set.

## Clients

| Client | Path | Coverage | Last validated | Notes |
| --- | --- | --- | --- | --- |
| macOS | App Store or standalone Tailscale | Automated (route classification) + Manual | _pending_ | Route output is best-effort on macOS; `utun*` is reported as possible-tailscale |
| Linux | Normal TUN mode (`tailscale0`) | Automated + Manual | _pending_ | |
| Linux | Userspace networking warning path | Automated | n/a | Unit test asserts the warning is emitted |

## Modes

| Mode | Coverage | Last validated | Notes |
| --- | --- | --- | --- |
| App Connector only | Automated (diagnose + client route) + Manual | _pending_ | Baseline domain expected to stay local |
| Same-region connector failover | Automated (monitor logic) + Manual | _pending_ | See [Failover.md](Failover.md) |
| Exit-node fallback enabled | Automated (enable helper) + Manual | _pending_ | Baseline traffic expected via exit node |
| Exit-node fallback disabled | Automated (disable helper) + Manual | _pending_ | Connector tag must remain present |
| Exit-node primary/fallback failover | Automated (controller + health engine) + Manual | _pending_ | macOS/Linux controller; see [Failover.md](Failover.md) |
| Advanced policy plan/apply-plan/restore | Automated (mocked API) + Manual (one real read-capable credential, `plan` only — no write) | _pending_ | See [Tailscale-API-mode.md](Tailscale-API-mode.md) |

## What CI already covers

These run on every push (no real infrastructure needed):

- Shell syntax (`bash -n`) and `shellcheck` for all entrypoints.
- Python unit tests: policy parsing/merge/validate, plan/apply-plan/restore against a
  mocked Tailscale API, secret redaction, and domain normalization.
- Diagnostics fake-command tests for `diagnose.sh` and `check-client-routes.sh`
  across connector-only, exit-node, userspace-networking, and failure states on
  both macOS and Linux `uname` paths.
- Exit-node enable/disable and connector-restore helpers via fake `tailscale`.
- The failover health engine, controller locking and switching, live-state
  reconciliation, fail-closed behavior, numeric configuration bounds, and
  connector monitor via fake `tailscale`.
- Credential-free smoke test, docs link check, packaging check, and syntax/lint
  coverage for the real-environment evidence collector.

The Manual rows above are what remains for a human to confirm on real hardware
before tagging a release.

## Release validation log

### v1.1.0 release candidate - 2026-06-10

- Local platform: macOS, Python 3.11.
- Passed shell syntax and `shellcheck` for all release-checklist scripts.
- Passed 300 unit tests with `python3 -B -m unittest discover -s tests`.
- Passed credential-free smoke tests, docs link checks, and
  `scripts/package.sh --check`; both release archives passed checksum and
  content verification.
- Failover safety regressions cover unavailable or non-running Tailscale state,
  ambiguous/recreated node identity, route authority, controller lock liveness,
  bounded configuration, failed readback/state persistence, and observe-only
  operation.
- Live App Connector and exit-node failover runs remain **pending** because they
  require real connector hosts, clients, and a live tailnet.

### v1.0.0 release candidate - 2026-06-08

- Local platform: macOS, Python 3.11.
- Passed shell syntax and `shellcheck` for all release-checklist scripts.
- Passed 143 unit tests with `python3 -m unittest discover -s tests`.
- Passed credential-free smoke tests, bootstrap common-domain dry run,
  docs link checks, and `scripts/package.sh --check`.
- Identity safety regressions cover ambiguous Self tags, stale persisted
  identity, partial explicit identity, coherent environment/region precedence,
  non-interactive reset acknowledgement, and malformed status JSON before and
  after helper mutations.
- Fresh-VPS Guided Mode and an existing JP connector regression remain
  **pending** because they require real Linux hosts and a live tailnet.

## Run capture template

```text
### <provider> / <client> / <mode> — YYYY-MM-DD

- Provider: <name, region, plan>
- OS: <output of cat /etc/os-release or sw_vers>
- Tailscale version: <output of tailscale version>
- Toolkit version: <output of ./bootstrap.sh --version>

diagnose.sh --json:
<paste>

check-client-routes.sh --json:
<paste>

Known limitations / observations:
- <notes>
```
