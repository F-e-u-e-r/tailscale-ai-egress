# Security Policy

This project bootstraps a personal Tailscale AI App Connector on infrastructure
you control. It is designed to be safe by default: manual policy setup is the
primary path, and policy automation is Advanced Mode only.

## Supported Versions

Security fixes target the latest released 1.x version. Please reproduce on the
latest tag before reporting.

| Version | Supported |
| --- | --- |
| 1.x (latest) | Yes |
| < 1.0 (pre-release) | No |

## Reporting a Vulnerability

Please do not open a public issue for security problems.

Report privately using GitHub's **"Report a vulnerability"** flow:

- https://github.com/F-e-u-e-r/tailscale-ai-egress/security/advisories/new

When reporting, include:

- The version (`./bootstrap.sh --version` or any entrypoint `--version`).
- A description of the issue and its impact.
- Steps to reproduce, ideally with redacted output.
- Any suggested remediation.

Do not include live secrets (auth keys, API tokens, OAuth client secrets) in
your report. The toolkit redacts known token patterns from its own output, but
please double-check anything you paste.

You can expect an acknowledgement on a best-effort basis. Because this is a
community project with no commercial backing, there is no guaranteed response
time, but credible reports are taken seriously and triaged as quickly as
practical. Coordinated disclosure is appreciated: please give a reasonable
window for a fix before any public write-up.

## What This Tool Does

- Installs Tailscale on a supported Linux VPS when it is missing.
- Installs basic diagnostics dependencies through the host package manager.
- Enables IPv4 forwarding, and IPv6 forwarding when the host supports it.
- Runs `tailscale up` with `--advertise-connector` and the configured tag.
- Generates Tailscale policy snippets for manual paste into the Admin Console.
- Runs local VPS-side and client-side diagnostics.

## What This Tool Does Not Do

- Does not require a Tailscale API token for the default manual path.
- Does not modify your tailnet policy unless you explicitly choose Advanced Mode.
- Does not collect telemetry.
- Does not upload logs.
- Does not proxy traffic through servers owned by this project.
- Does not store API keys or auth keys in repo files.
- Does not communicate with external services beyond package repositories,
  Tailscale services, public IP/DNS checks, and the Tailscale API when you
  explicitly opt into Advanced Mode. See [PRIVACY.md](PRIVACY.md) for the exact
  list of endpoints.

## No Telemetry, No Project Servers

There is no analytics, phone-home, crash reporting, or update check. The project
operates no servers. Every network call is either to your own infrastructure,
your OS package mirrors, Tailscale's own services, or well-known public IP/ASN
lookups used by diagnostics. The full inventory is in [PRIVACY.md](PRIVACY.md).

## Secret Handling

- Use a one-off Tailscale node auth key for each VPS bootstrap.
- Use `tskey-auth-...` for node registration, not `tskey-api-...`.
- API tokens or OAuth credentials are only for Advanced Policy Automation.
- Interactive prompts read secrets with hidden terminal input.
- When a node auth key is provided, `bootstrap.sh` writes it to a temporary
  `0600` file and passes it to `tailscale up` with `--auth-key=file:...` so the
  secret is not exposed in shell history or process arguments.
- Temporary auth-key files are removed on normal exit and interrupt signals.
- The policy tool redacts `Authorization` headers and `tskey-*` patterns from
  all error and network output.
- Pass credentials via environment variables or hidden prompts. Never commit
  them to the repo or paste them into issues.

## Connector Identity Metadata

After a successful bootstrap, `generated/connector-identity.env` stores only the
connector region, tag, and hostname. It contains no auth key or API credential,
is git-ignored, and is written with mode `0600` when supported.

Destructive connector repair fails closed: `restore-connector.sh --force-reset`
requires a complete identity from one source and refuses partial environment
overrides, stale persisted identity, or multiple `tag:ai-egress-*` tags before
running `tailscale up --reset`. The non-reset restore path remains conservative
and does not use persisted identity to rewrite hostname or tags.

## Token Scope Recommendations

Node auth keys (`tskey-auth-...`), used by `bootstrap.sh`:

- Non-reusable (one key per VPS), short expiration (1-7 days).
- Not ephemeral for a persistent connector, so the node stays in the tailnet.
- Tagged with the generated connector tag (for example `tag:ai-egress-us`).

Tailscale API access, used only by Advanced Policy Automation
(`policy_tool.py plan` / `apply-plan` / `restore-plan`):

- Prefer an **OAuth client** over a long-lived personal API key. OAuth client
  credentials mint short-lived access tokens automatically.
- The essential scope is `policy_file` (read and write the tailnet policy). The
  tool's default OAuth scope string also requests read-only `devices:core:read`
  and `devices:posture_attributes:read`, which the policy preview endpoint can
  use; grant only what your flow needs and override with
  `TAILSCALE_OAUTH_SCOPES` if you want to narrow it.
- Scope the OAuth client narrowly and rotate it. Treat policy-write access as
  high privilege: it can change routing and access for the whole tailnet.

## Advanced Policy Automation

Advanced Mode can fetch, merge, validate, and apply your tailnet policy through
the Tailscale API. It writes an auditable plan bundle (including the original
`current.hujson` and SHA-256 integrity checks) and asks for an exact
`APPLY <plan-id>` confirmation before applying. Use this only when you
understand the policy change being made.

The generated policy grants `autogroup:member` access to `autogroup:internet`
and auto-approves `0.0.0.0/0` and `::/0` routes for the connector tag. Protect
ownership of `tag:ai-egress-*` carefully, since it controls broad egress routing.

`POLICY_RISK_ACK=1` skips the interactive policy risk confirmation and is meant
for CI or scripted deployments only.
