# Privacy

This toolkit runs entirely on infrastructure you control. It has **no
telemetry, no analytics, no phone-home, no update check, and no
project-operated servers**. Nothing about your usage is sent to the project
authors.

This document lists every external service the scripts can contact, so you can
audit egress before running them. "External" means anything off the local
machine. Everything the scripts write stays under the repo's `generated/`
directory.

## During install (`bootstrap.sh` / `install.sh`)

| Endpoint | Why | When |
| --- | --- | --- |
| Your OS package mirrors (`apt`/`dnf`/`yum`/`apk`) | Install `curl`, `python3`, `dig`, `traceroute`, `whois`, `iproute2`, `ethtool`, etc. | Always, unless deps already present |
| `https://tailscale.com/install.sh` | Tailscale's official Linux installer, piped to `sh` | Only when `tailscale` is not already installed |
| Tailscale control plane (e.g. `login.tailscale.com`, `controlplane.tailscale.com`, DERP relays) | `tailscale up` registers the node and joins your tailnet | Always |
| GitHub release downloads (`github.com/.../releases/download/...`, often redirected to GitHub's release asset storage) | Downloads the tagged source artifact and `SHA256SUMS` | Only if you run `install.sh` outside a checkout without `TAILSCALE_AI_EGRESS_BRANCH` |
| GitHub branch archive (`codeload.github.com` via `install.sh`) | Downloads an unverified development branch tarball | Only if you run `install.sh` outside a checkout with `TAILSCALE_AI_EGRESS_BRANCH` |
| GitHub Attestations API (`api.github.com`) and TUF trust-root hosts (`tuf-repo.github.com`, `tuf-repo-cdn.sigstore.dev`, `tmaproduction.blob.core.windows.net`) | `gh attestation verify` fetches the release's build-provenance attestation and the Sigstore/TUF trust roots that validate it | Only on a release install when an authenticated `gh` >= 2.93.0 is present and `TAILSCALE_AI_EGRESS_SKIP_ATTESTATION=1` is not set |
| `https://ifconfig.co/country-iso` | Detect the VPS country code for the derived connector identity | Only in non-dry-run `bootstrap.sh` when `REGION` is unset and `curl` or `wget` is available |
| `https://ifconfig.co` | The post-install `diagnose.sh` run (see below) | At the end of a non-dry-run bootstrap |
| `https://api.tailscale.com` | Advanced Mode policy automation (see below) | Only if you explicitly opt into Advanced Mode |

`install.sh` is a thin wrapper. By default it verifies downloaded release
artifacts against `SHA256SUMS`; branch archives are intentionally called out as
unverified. When it invokes `gh` for the optional attestation verification, it
sets `GH_TELEMETRY=false` and `GH_NO_UPDATE_NOTIFIER=1` on every call, so gh
sends no telemetry and performs no update check on this project's behalf. For stricter provenance, clone a reviewed tag and run
`./bootstrap.sh` directly instead of piping `install.sh` from the network. See
[docs/Stability.md](docs/Stability.md).

## During diagnostics

`diagnose.sh` (VPS host):

| Endpoint | Why |
| --- | --- |
| `https://ifconfig.co/ip` and `https://ifconfig.co/asn` | Report the VPS public IPv4/IPv6 egress address and ASN |
| Your DNS resolver | Resolve a small sample of configured AI domains to check routing |

`check-client-routes.sh` (client device):

| Endpoint | Why |
| --- | --- |
| Your DNS resolver | Resolve the configured AI domains and the baseline domain (`ipinfo.io` by default) |

`check-client-routes.sh` does **not** make HTTP requests. It only performs DNS
lookups and reads the local routing table (`route get` / `ip route get`). The
baseline domain is resolved and route-checked, not fetched.

## During Advanced Mode (`scripts/policy_tool.py`)

Advanced Policy Automation is opt-in. When used, it contacts only the Tailscale
API:

| Endpoint | Why |
| --- | --- |
| `https://api.tailscale.com/api/v2/oauth/token` | Exchange OAuth client credentials for a short-lived token (OAuth flow only) |
| `https://api.tailscale.com/api/v2/tailnet/<tailnet>/acl` | Fetch and apply the tailnet policy |
| `https://api.tailscale.com/api/v2/tailnet/<tailnet>/acl/validate` | Validate the merged policy |
| `https://api.tailscale.com/api/v2/tailnet/<tailnet>/acl/preview` | Preview affected rules (best-effort) |

`rollback.sh` contacts `https://api.tailscale.com` only when API/OAuth
credentials are present; without credentials it prints a backup path and the
Admin Console URL for manual restore and makes no external request. The
exit-node and connector helper scripts (`enable-exit-node.sh`,
`disable-exit-node.sh`, `restore-connector.sh`) only call the local `tailscale`
CLI and do not contact the project or any extra third party.

## Data handling

- Credentials are read from environment variables or hidden terminal prompts and
  are never written to repo files. Known token patterns are redacted from output.
- Generated artifacts are written under `generated/`, which is git-ignored.
  Policy snippets, plan bundles, and backups can contain your tailnet policy;
  `connector-identity.env` contains the connector region, tag, and hostname.
  Treat generated files as sensitive and do not commit or share them publicly.
- The project authors receive nothing. Any third-party service listed above is
  governed by that party's own privacy policy (Tailscale, your VPS provider,
  your OS vendor, GitHub, and `ifconfig.co`).
