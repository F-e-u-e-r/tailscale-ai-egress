# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Version numbers refer to the toolkit as a whole. The single source of truth is
the [`VERSION`](VERSION) file, mirrored by `__version__` in
[`scripts/policy_tool.py`](scripts/policy_tool.py) and
[`scripts/health_check.py`](scripts/health_check.py). Every shell entrypoint and
both Python CLIs report it with `--version`.

## [Unreleased]

### Added

- `diagnose.sh`: `--connector-tag <tag>` plus custom connector-tag detection.
  The expected connector tag is resolved by precedence — `--connector-tag`, then
  the `CONNECTOR_TAG` environment variable, then
  `generated/connector-identity.env`, then the historical `tag:ai-egress-*`
  convention — so hosts using a non-`ai-egress-*` tag are detected. Behavior is
  unchanged when no tag is provided.
- `failover-exit-node.sh`: opt-in `FAILOVER_NOTIFY_CMD` hook, run after a real
  switch attempt with `FAILOVER_EVENT` (`switched`|`failed`), `FAILOVER_ROLE`,
  `FAILOVER_LABEL`, and `FAILOVER_REASON` in the environment. Environment only
  (not parsed from `failover.env`); its exit status is ignored and cannot change
  the switch outcome.
- Per-connector metrics (counters + liveness), read-only. `scripts/health_check.py`
  gains a `peer-metrics --node <label>` subcommand printing a fixed-shape JSON
  object (tx/rx byte counters, online/active, last-handshake age, relay/cur_addr,
  and a derived `connection_path`), and the `connectors` report (used by
  `monitor-connectors.sh`) now includes a `metrics` object per connector in
  `--json` plus an append-only `[metrics]` text line. Additive only; metrics never
  gate the monitor's health verdict. Byte counters are cumulative since tailscaled
  started, not billing usage. See `docs/design/metrics-collection.md`.

### Changed

- Internal refactor (no behavior change): `enable-exit-node.sh`,
  `disable-exit-node.sh`, and `restore-connector.sh` now source the shared
  `scripts/lib/common.sh` for `run_root` (as `ai_egress_run_root`), completing the
  first helper of the shared-shell-library migration across all three consumers.
  `scripts/lib/common.sh` is therefore a runtime dependency of those scripts and
  is verified in the release package. CI lints with `shellcheck -x`.

## [1.1.1] - 2026-07-08

Patch release: correctness, consistency, and hardening fixes from an external
review. No CLI surface changes; every entrypoint and both Python CLIs keep their
1.1 behavior and flags.

### Fixed

- `bootstrap.sh`, `enable-exit-node.sh`, `restore-connector.sh`: the
  `read_line`/`read_secret` `/dev/tty` fallback now prints the prompt to
  `/dev/tty` (mirroring `rollback.sh`) instead of relying on `read -p`, whose
  prompt goes to stderr and was swallowed when stdin is piped but a controlling
  terminal is available (e.g. `curl … | bash`). Security-relevant prompts (auth
  key, Advanced Mode confirmation, `APPLY <plan-id>`) are now visible again.
- `scripts/health_check.py`: URL-encode the tailnet in the devices API path so a
  tailnet name containing reserved characters cannot rewrite the request path;
  matches `policy_tool.tailnet_path()`.
- `failover-exit-node.sh`: remove a stale controller lock atomically (rename
  then delete) so two controllers that simultaneously judge a lock stale cannot
  both enter the apply cycle (TOCTOU).
- `bootstrap.sh`: match the running connector hostname as an exact
  whitespace-delimited field (via `awk`) so `ai-egress-jp-01` no longer matches
  a different host such as `ai-egress-jp-011` or `foo-ai-egress-jp-01` and skips
  `tailscale up` in non-interactive mode.
- `policy/app-connector.example.json`: add `notebooklm.google.com` to match
  `policy/default-ai-domains.json`.

### Changed

- `scripts/policy_tool.py`: policy plan bundles and backups are now written with
  private permissions (0700 directories, 0600 files) instead of at the process
  umask, since both contain the full tailnet policy.
- `monitor-connectors.sh`: clarify in `--help` that `TAILSCALE_API_KEY` is read
  from the environment only and is not parsed from `generated/failover.env`.
- `install.sh`: print a note to stderr when it executes a local checkout, so the
  local-vs-remote install path is visible.
- CI: add Python 3.13 to the test matrix.

## [1.1.0] - 2026-06-10

Adds optional, opt-in high-availability tooling: a ping-driven exit-node
failover controller and a connector high-availability monitor. The 1.0 command
surface is unchanged; everything here is additive and off by default.

### Added

- `scripts/health_check.py`: a standard-library health/ping engine with `probe`
  (tailnet ping plus an optional short-timeout HTTP egress check), `verdict`
  (stateful primary/fallback decision with hysteresis, cooldown, and a
  fallback-verified decision matrix), `record-switch`, `active-role`, and
  `connectors` subcommands.
- `failover-exit-node.sh`: client-side (macOS + Linux) exit-node failover
  controller. Observe-first; it mutates `tailscale set --exit-node` only with
  `--apply`, behind a controller-level lock, with live-state reconciliation and
  a post-switch readback. `RESTORE_PRIMARY=0/1` controls auto-switch-back.
- `monitor-connectors.sh`: read-only App Connector high-availability monitor
  (online plus reachability, and optional `device.created` ordering when an API
  token is configured; it degrades gracefully without one and never switches).
- `examples/failover.env.example` plus `docs/examples/` systemd, launchd, and
  cron units for running the controller and monitor automatically.
- Failover documentation: oldest-first native connector primary selection and
  the new exit-node failover flow in [docs/Failover.md](docs/Failover.md).

### Hardening

- Strict, bounded configuration validation for `failover-exit-node.sh`,
  `monitor-connectors.sh`, and `health_check.py`: non-numeric, out-of-range, or
  absurdly large values (a bare `.`, `nan`/`inf`, and seconds values above the
  `86400`-second/one-day cap) are rejected with a clear error before any probing
  or switching. Environment-variable defaults are validated the same way as
  command-line flags, and `TAILSCALE_STATUS_TIMEOUT` is bounded to `(0, 86400]`.
- Fail-closed safety: a missing/malformed `BackendState` or any non-`Running`
  backend is treated as "do not switch", including the post-switch readback, so a
  stopped backend can no longer be recorded as a successful switch.
- Robust controller lock and apply path: the lock holder is identified by process
  start time so a recycled PID cannot make a stale lock block failover forever; a
  failing interval/readback sleep refuses to busy-loop; and a state-persistence
  failure after a switch is reported as a non-zero result, not a false success.

### Notes

- App Connector failover stays Tailscale-native (oldest connector = primary,
  oldest-first, all plans); this release does not reassign connectors via the
  policy API.

## [1.0.0] - 2026-06-08

First stable release. The goal of 1.0 is release quality rather than new routing
features: a frozen command surface, tested provider/client matrix, complete
documentation, a clear security and privacy posture, and reproducible release
artifacts.

### Stability guarantees (frozen for the 1.x series)

- Stable CLI surface: `bootstrap.sh`, `check-client-routes.sh`, `diagnose.sh`,
  `enable-exit-node.sh`, `disable-exit-node.sh`, `restore-connector.sh`,
  `rollback.sh`, and `scripts/policy_tool.py`.
- Policy-tool plan manifest schema major version is frozen at `1`. Plan bundles
  generated by 0.4 remain readable by 1.x tooling.
- The built-in domain list is frozen as `common`; custom domains use
  `--domains-file`.
- Manual Guided Mode remains the recommended path; Advanced Admin Automation
  stays opt-in only. See [docs/Stability.md](docs/Stability.md).

### Added

- `VERSION` file and `--version` on every shell entrypoint and `policy_tool.py`,
  all reporting a single source of truth.
- The built-in `common` domain list, including ChatGPT, OpenAI, Claude, Poe,
  OpenRouter, Perplexity, and NotebookLM domains. Custom domains are supported
  through `--domains-file` and the interactive wizard.
- Project metadata: `LICENSE` (MIT), `CHANGELOG.md`, `CONTRIBUTING.md`,
  `SUPPORT.md`, GitHub issue/PR templates.
- `PRIVACY.md` describing exactly which external services are contacted during
  install, diagnostics, and Advanced Mode.
- Release and quality docs: [docs/Stability.md](docs/Stability.md),
  [docs/Release-Checklist.md](docs/Release-Checklist.md),
  [docs/Validation-Matrix.md](docs/Validation-Matrix.md), and
  [docs/Uninstall.md](docs/Uninstall.md).
- "Which Mode Should I Use?" guidance in the README.
- `scripts/smoke-test.sh`: a credential-free smoke-test sequence that exercises
  the local-only and dry-run paths.
- `scripts/package.sh`: builds reproducible source `tar.gz` + `zip` artifacts
  with a generated `SHA256SUMS`.
- `scripts/check_docs_links.py`: offline Markdown link checker.
- `bootstrap.sh` now detects a default region from the VPS public IP in
  interactive runs, lets the user confirm or override it, and offers a short
  hostname keyword prompt. Automation, dry-run mode, and explicit identity
  environment variables remain deterministic.
- `bootstrap.sh` persists the resolved connector tag and hostname to
  `generated/connector-identity.env` after a successful setup so helper scripts
  can reuse the intended identity.
- Maintainer documentation and an idempotent `gh api` script for reproducing
  the `main` repository ruleset and merged-branch cleanup setting.
- Expanded CI (shell syntax, shellcheck, Python unit tests, policy-tool CLI
  smoke tests, diagnostics fake-command tests, docs link check, packaging check)
  and a tag-triggered release workflow for `v*`.

### Changed

- `README.md` trimmed toward a quick start, with advanced detail moved into
  `docs/`.
- `README.md` and `README.zh-HK.md` merge the overlapping "why / which mode /
  when to use" sections into one **Which Mode Should I Use?** section, collapse
  the Tailscale glossary and alternatives comparison behind `<details>`, and
  keep the Advanced Mode, Rollback, and Security Notes sections concise.
- `install.sh` now downloads tagged release artifacts and verifies
  `SHA256SUMS` by default; setting `TAILSCALE_AI_EGRESS_BRANCH` keeps the
  development branch archive path with an explicit unverified-download warning.
- `bootstrap.sh` avoids `tailscale up --reset` on fresh or logged-out nodes and
  attempts `restore-plan` if Advanced Mode applied a plan but connector startup
  then fails.
- Connector helpers now treat identity as one coherent source instead of mixing
  environment, persisted-file, and Tailscale-status fields.

### Fixed

- `diagnose.sh` and `check-client-routes.sh` no longer emit a spurious
  "unbound variable" message from the cleanup trap when invoked with `--version`
  or `--help` on Bash 3.2 (the default macOS Bash).
- `restore-connector.sh --force-reset` now refuses partial explicit identity,
  stale persisted identity, and multiple matching connector tags before running
  `tailscale up --reset`.
- Exit-node helpers now reject ambiguous or conflicting connector tags instead
  of silently selecting the first matching status tag.
- Malformed `tailscale status --json` output now fails closed before helper
  mutations and degrades to a warning during post-change verification without
  exposing a Python traceback.
- Invalid `diagnose.sh --domain-pack` values now exit immediately instead of
  continuing into diagnostics after printing a usage error.

## Prior to 1.0

These pre-release milestones were developed before the changelog was kept and
are summarized from project history.

### 0.4.0 - Policy plan workflow

- Auditable `plan` / `apply-plan` / `list-plans` / `restore-plan` workflow with
  plan bundles (`current.hujson`, `merged.json`, `diff.patch`, `manifest.json`,
  optional `api-preview.json`), SHA-256 integrity checks, and `ETag`/`If-Match`
  conflict detection. Introduced manifest schema version `1`.

### 0.3.0 - Policy tooling and JSON diagnostics

- `scripts/policy_tool.py` `snippet` / `validate` / `merge` / `diff` / `apply` /
  `restore`, HuJSON-tolerant parsing, domain normalization, broad-wildcard
  blocklist, and secret redaction. `--json` output for `diagnose.sh` and
  `check-client-routes.sh`.

### 0.2.0 - Fallback, failover, and translations

- `enable-exit-node.sh` / `disable-exit-node.sh` / `restore-connector.sh`
  helpers, same-region connector failover guidance, and Traditional Chinese
  (Hong Kong) documentation.

### 0.1.0 - Initial toolkit

- `bootstrap.sh` connector setup, `diagnose.sh`, `check-client-routes.sh`,
  `rollback.sh`, the common domain list, and Manual Guided Mode policy snippets.

[Unreleased]: https://github.com/F-e-u-e-r/tailscale-ai-egress/compare/v1.1.1...HEAD
[1.1.1]: https://github.com/F-e-u-e-r/tailscale-ai-egress/releases/tag/v1.1.1
[1.1.0]: https://github.com/F-e-u-e-r/tailscale-ai-egress/releases/tag/v1.1.0
[1.0.0]: https://github.com/F-e-u-e-r/tailscale-ai-egress/releases/tag/v1.0.0
