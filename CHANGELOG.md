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

## [1.2.0] - 2026-07-17

### Added

- **CI: bilingual README doc-drift check.** `scripts/check_readme_parity.py` (stdlib-only) fails if
  `README.md` and `README.zh-HK.md` do not mention the same set of entrypoints (the 10 top-level `*.sh`
  plus `policy_tool.py`/`health_check.py`), catching "added to one language's README, forgot the other."
  Matching is filename-bounded, and a `tests/test_docs.py` suite proves it catches drift in both directions.
  Wired into CI on the 3.12 Linux leg.
- **Dependabot for the pinned GitHub Actions.** `.github/dependabot.yml` (weekly, grouped, `ci`-prefixed)
  keeps the SHA-pinned actions current; the version annotations in `ci.yml`/`release.yml` were moved inline
  (`uses: …@sha # vX.Y.Z`) so Dependabot updates the SHA and the comment together.
- **OpenRC service examples for Alpine.** `docs/examples/openrc/{failover-exit-node,monitor-connectors}`
  (mode 0755) run the failover controller / read-only HA monitor under `supervise-daemon` — the closest
  OpenRC analogue to systemd `Restart=` — with `need net`/`need tailscale` ordering, a `start_pre`
  interpreter check, and `required_files` (wrapper + health engine) so a missing dependency is a loud
  `rc-service start` failure rather than a silent respawn loop. Optional `*.confd` overrides ship alongside;
  `docs/examples/README.md` documents the prerequisites, install/enable/start commands, and a `logrotate`
  (`copytruncate`) stanza for the append-only service logs. `scripts/package.sh --check` now asserts all four
  files ship and the two init scripts are executable, and a `test_openrc_examples_are_valid` test locks the
  `sh -n` syntax, exact `command_args`, and the crash-loop safety fields.
- `install.sh`: **optional build-provenance attestation verification** for release
  downloads. When [GitHub CLI](https://cli.github.com/) `gh` (>= 2.93.0) is present, the
  installer runs `gh attestation verify` on the downloaded release tarball after the
  `SHA256SUMS` checksum, narrowed to this repo's `.github/workflows/release.yml` on the
  exact `refs/tags/<tag>` at `github.com`. A missing, older, unrecognized, or
  unauthenticated `gh` degrades to checksum-only and never fails for that reason
  (`gh attestation verify` requires a github.com credential even for public repos and
  exits with gh's documented authentication code 4; the token-leak GHSA-8xvp-7hj6-mcj9 and
  false-pass GHSA-fgw4-v983-mgp8 advisories are avoided by requiring 2.93.0); an attestation
  an authenticated `gh` actively rejects is fatal. `TAILSCALE_AI_EGRESS_SKIP_ATTESTATION=1` opts out
  (offline installs or a non-GitHub mirror) — the checksum still runs, publisher provenance
  does not. The release workflow already produces these attestations, so no `release.yml`
  change was needed; the unverified `TAILSCALE_AI_EGRESS_BRANCH` path is unaffected.
- `check-client-routes.sh`: an **advisory IPv6 route check**. When a selected AI domain
  (or the baseline) publishes AAAA records, the checker resolves them and inspects the
  IPv6 route (`route -n get -inet6` on macOS, `ip -6 route get` on Linux) under new
  `*-ipv6` check ids (`ai-domain-route-ipv6`, `baseline-route-ipv6`,
  `ai-route-summary-ipv6`). IPv6 findings are advisory only — a mismatch is a `WARN`,
  never a `FAIL`, and a domain with no AAAA is skipped cleanly — so the script's exit code
  and the `schema_version: 1` JSON shape are unchanged and IPv4 stays the pass/fail signal.
- `scripts/policy_tool.py`: broad CDN / shared-infrastructure domains
  (`cloudfront.net`, `amazonaws.com`, `googleusercontent.com`, `azureedge.net`,
  `akamaihd.net`, `fastly.net`) now `validate` with a `broad-wildcard-warning` (they can
  route unrelated shared-CDN traffic through the connector); the separate broad-wildcard
  blocklist still fails closed unless `--allow-broad-wildcard`. Warnings do not change the
  exit code.
- **macOS CI job.** A parallel `macos-latest` workflow job covers what the Linux
  matrix cannot: stock Bash 3.2 syntax (`/bin/bash -n`), the BSD userland (a real
  `route -n get` probe, the `stat -f` lock-age arm exercised by the failover
  stale-lock tests, and the `shasum -a 256` packaging fallback forced via a
  restricted `PATH`), plus the full test suite on Darwin. It deliberately does not
  re-run shellcheck / ruff / mypy / docs-links / coverage (platform-independent,
  already covered by the Linux job). Also adds a `diagnose.sh` Darwin `route -n get`
  test mirroring the client-route checker, and makes both route-checker fake `route`
  commands argument-strict. Advisory only (not a required status check); no script,
  behavior, CLI, or schema change.
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
- Per-connector metrics gain a `latency_ms` field (the `tailscale ping` RTT),
  stamped only on a resolved peer. The `connectors` report reuses its existing
  reachability ping (no extra ping) and appends `latency_ms=` to the end of the
  `[metrics]` text line; `scripts/health_check.py peer-metrics` gains an opt-in
  `--ping` flag to measure it standalone. Additive and still non-gating.
- `monitor-connectors.sh --prometheus-textfile <path>`: write a node_exporter
  textfile of per-connector gauges (online/reachable/latency/tx/rx/handshake-age/
  routes/connection-path + an `ai_egress_overall_healthy` sentinel) instead of the
  normal report. The document is built and written atomically by
  `scripts/health_check.py connectors --prometheus [--output <path>]` (a new,
  `--json`-mutually-exclusive mode). Null/non-finite/negative/malformed values are
  omitted (never a fake `0`); the write exit code reflects write success, not
  health. With `--watch` the file is rewritten each interval and a failed write
  leaves the previous file intact.
- Docs: "Post-Switch Diagnostics With `peer-metrics`" (`docs/Failover.md`) shows how
  to log the new exit node's metrics after a failover switch by composing the
  existing `FAILOVER_NOTIFY_CMD` hook with `peer-metrics --ping` — no controller
  change. The switch is recorded before the hook runs; the hook is synchronous, so
  the usual "keep it fast" caveat applies (a `SIGTERM` during it still yields `143`).

### Changed

- **CI coverage floor ratcheted 55% → 70%** (`.github/workflows/ci.yml`). Added targeted
  `tests/test_policy_tool.py` tests for previously-uncovered `policy_tool` API error paths (OAuth-token
  fallback, missing-credential, and the bearer→basic 401 retry), nudging `policy_tool.py` coverage up; TOTAL
  is ~77%.
- Internal refactor (no behavior change): `enable-exit-node.sh`,
  `disable-exit-node.sh`, and `restore-connector.sh` now source the shared
  `scripts/lib/common.sh` for `run_root` (as `ai_egress_run_root`), completing the
  first helper of the shared-shell-library migration across all three consumers.
  `scripts/lib/common.sh` is therefore a runtime dependency of those scripts and
  is verified in the release package. CI lints with `shellcheck -x`.

### Removed

- `scripts/policy_tool.py`: the deprecated direct **`apply`** command has been
  removed. It printed a runtime "deprecated; will be removed after one release"
  warning since v1.0, and v1.1.0 has shipped, so per [Stability](docs/Stability.md)'s
  allowed-exception policy it is gone. `apply` is now a migration tombstone: any
  non-help invocation (including old flag combinations) exits non-zero with a pointer to the
  auditable workflow — create a bundle with `plan`, review it, then `apply-plan
  <plan-dir>`. `plan`, `apply-plan`, `restore`, and `restore-plan` are unchanged;
  `rollback.sh` still restores any timestamped backups the former `apply` created.

### Fixed

- Docs: the Quick Start lists Alpine as supported, but the wrappers are Bash while
  Alpine ships only BusyBox `ash`, so `./bootstrap.sh` failed on a stock Alpine host.
  `README.md`, `README.zh-HK.md`, and `docs/Generic-VPS.md` now note `apk add bash git`
  as an Alpine prerequisite before cloning.
- `scripts/health_check.py`: reading a `--status-json-file` that is not valid UTF-8
  now fails closed (treated as unavailable status) instead of raising
  `UnicodeDecodeError`, so `peer-metrics` keeps its always-exit-0 contract and the
  failover/monitor commands degrade cleanly.
- `scripts/health_check.py`: malformed-status hardening. A non-list `TailscaleIPs`
  (or `AllowedIPs`) no longer crashes `resolve_identity` / `_find_node` /
  `node_routes` / `live_active_role` — such fields are read via shared coercion, so a
  scalar yields no identity and a dict is no longer iterated as keys (which had let a
  malformed `ExitNodeStatus.TailscaleIPs` mis-attribute the live exit-node role on the
  failover path). Every status-derived identity/gating IP list -- in `resolve_identity`,
  `_find_node`, `live_active_role`, and `node_routes` -- is validated per element
  through one shared strict parser, **whole-field fail-closed** (any invalid element
  voids the entire list rather than keeping the valid ones), so a value like
  `100.64.0.1/not-a-prefix`, a dotted-netmask form (`100.64.0.1/255.255.255.255`), an
  IPv6 zone id (`fd7a::1%zone`), or an over-long prefix — all of which `ipaddress`
  accepts (or crashes on) but Tailscale never emits — can no longer be truncated,
  coerced into a false address / route match, or raised as an unhandled error. `live_active_role` now distinguishes an absent/null
  `ExitNodeStatus` (legitimately `none`) from a present-but-malformed one (fails
  closed to `unknown`), so a garbled status can never authorize a switch under
  `--ensure-primary`. `node_routes` validates each advertised route with `ipaddress`
  and fails closed (returns "unknown") on a wrong-type field or any invalid/empty
  element; a present-but-null or absent field stays authoritatively empty (a Go nil
  slice marshals to `null`), and in the `AllowedIPs` fallback an untrustworthy
  `TailscaleIPs` (non-list, invalid-IP element, or empty) fails closed rather than
  counting the connector's own address as a route. This one strict result backs the
  JSON `routes` field, the `serving` / `overall_healthy` verdict, and the Prometheus
  `_routes` gauge, so they agree (a malformed route field degrades the pair rather
  than being counted). Because addresses are compared by their canonical form, an
  IP-valued connector label and the persisted failover state also survive an
  equivalent re-spelling (expanded vs compressed IPv6, case): the same node is not
  mistaken for a new one, so health history and a due failover are preserved across an
  upgrade or a label re-spelling (a native IPv4 and its IPv6-mapped form stay
  distinct). The `connectors` report also resolves each label once and
  reuses it for metrics extraction, so an ambiguous label warns once instead of
  twice. Real `tailscale status --json` is unaffected — own-address exclusion stays
  prefix-agnostic, matching prior behavior; these only change behavior on
  adversarial / malformed status.

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

[Unreleased]: https://github.com/F-e-u-e-r/tailscale-ai-egress/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/F-e-u-e-r/tailscale-ai-egress/releases/tag/v1.2.0
[1.1.1]: https://github.com/F-e-u-e-r/tailscale-ai-egress/releases/tag/v1.1.1
[1.1.0]: https://github.com/F-e-u-e-r/tailscale-ai-egress/releases/tag/v1.1.0
[1.0.0]: https://github.com/F-e-u-e-r/tailscale-ai-egress/releases/tag/v1.0.0
