# Roadmap

This is a **non-binding** planning document for direction beyond v1.1.1. It does
not promise dates or delivery. The binding contract is [Stability](Stability.md):
within 1.x nothing under its "Frozen surface" changes incompatibly, and GUI /
provider provisioning / telemetry / additional proxy protocols stay out of scope.

Every item below is tagged so the compatibility impact is explicit up front.

## Legend

| Tag | Meaning |
|---|---|
| `1.x additive-safe` | New optional flags, new docs/examples, new CI jobs, new source-only internal libraries, new output modes, or new scripts. No change to existing behavior or JSON schema. Allowed any time in 1.x. |
| `1.x allowed exception` | User-visible but explicitly permitted by [Stability](Stability.md) (e.g. removing the deprecated `policy_tool.py apply`). Needs an acceptance gate and a CHANGELOG note. |
| `2.0-only` | Changes the CLI contract or a frozen surface; must wait for a major release. |
| `out of scope` | Excluded from the project's trust boundary. |

---

## Now — v1.2 candidates (`1.x additive-safe` unless noted)

Highest user value first.

- **Custom connector-tag detection in `diagnose.sh`.** `1.x additive-safe`.
  Today diagnostics assume the `tag:ai-egress-*` convention. Prefer the tag from
  `generated/connector-identity.env`, then a `--connector-tag` flag, then fall
  back to the convention.
  *Acceptance:* diagnose works against a non-`ai-egress-*` tag with no regression
  to the default path; covered by fake-command tests.
- **`FAILOVER_NOTIFY_CMD` hook.** `1.x additive-safe`.
  Opt-in command run on switch success/failure, with `role` / `label` / `reason`
  passed via the environment. No telemetry, no server — just a user command.
  *Acceptance:* fires on switch with the documented environment; absence is a
  no-op; a failing hook never changes the switch outcome.
- **macOS CI job.** `1.x additive-safe`, high maintainer value — **land early.**
  A `macos-*` runner running `bash -n`, the smoke test, and a shell-test subset
  to cover Bash 3.2 + BSD userland (`stat -f`, `route -n get`) that only
  fake-command tests exercise today. This protects the shared-shell-library
  migration (see design docs) as real shell code starts moving.
  *Acceptance:* a green macOS job on the matrix; catches a BSD-only regression.
- **Broad-wildcard blocklist + warn list.** `1.x additive-safe`.
  Keep the fail-list, add a warn-list for CDN/infra wildcards
  (`cloudfront.net`, `amazonaws.com`, `googleusercontent.com`, `azureedge.net`,
  `akamaihd.net`, `fastly.net`, …) that warn rather than block.
  *Acceptance:* warned domains validate with a warning; blocked ones still fail;
  tests for both.
- **IPv6 route check in `check-client-routes.sh`.** `1.x additive-safe`.
  Add AAAA + `ip -6 route get` as a new check id; JSON schema only gains fields.
  *Acceptance:* new check id present; existing `schema_version: 1` consumers
  unaffected.
- **`install.sh` optional attestation verify.** `1.x additive-safe`.
  When `gh` is present, run `gh attestation verify` on the downloaded release,
  using the attestation the release workflow already produces.
  *Acceptance:* verify runs when `gh` exists; absence degrades gracefully.
- **OpenRC service example.** `1.x additive-safe`.
  `docs/examples/` currently has systemd/launchd/cron; bootstrap supports Alpine,
  so add an OpenRC unit.
  *Acceptance:* an OpenRC service file under `docs/examples/` that starts the
  controller/monitor on Alpine, referenced from the examples README.
- **Remove deprecated `policy_tool.py apply`.** `1.x allowed exception`.
  [Stability](Stability.md) already flags it for removal; the CLI has printed a
  "deprecated; will be removed after one release" warning since v1.0, and v1.1.0
  has shipped. Use `plan` + `apply-plan`.
  *Acceptance:* `apply` is gone with a clear pointer to `plan`/`apply-plan`; a
  test asserts the helpful error; CHANGELOG documents the removal.

---

## Next — v1.3 (`1.x additive-safe`)

- **Connector-failover apply mode (flagship).** `1.x additive-safe` (new opt-in
  script / flag). Design-first — see
  [design/connector-failover-apply.md](design/connector-failover-apply.md).
  Reuses the exit-node controller's observe-first / hysteresis / cooldown /
  fail-closed / readback skeleton and the auditable `plan` / `apply-plan`
  pipeline. Open question in the design doc: a new `failover-connectors.sh
  --apply` (leaning toward this, to preserve the monitor's "never switches"
  promise) vs. extending `monitor-connectors.sh`.
  *Acceptance:* observe-only by default; `--apply` produces an auditable plan
  bundle via `plan`/`apply-plan` and honors the design doc's fail-closed matrix;
  fake-command tests cover the no-apply, apply, and fail-closed paths.
- **Multi-fallback support.** `1.x additive-safe`.
  `FALLBACK_EXIT_NODE` accepts a comma-separated list tried in order; state
  schema adds `nodes.fallbacks[]` and bumps `schema_version` to 2 while keeping
  v1 read-compatibility. Coordinate the bump with apply mode's state changes.
  *Acceptance:* an ordered fallback list is tried in order; `schema_version: 2`
  state is written and pre-existing v1 state still reads; tests cover ordered
  fallback and the v1 read-compat path.
- **Monitoring integration.** `1.x additive-safe`.
  Step 1 (**done**): per-connector counters + liveness in the monitor's `--json`
  (`metrics` object) and an append-only `[metrics]` text line, plus a
  `health_check.py peer-metrics` subcommand — see
  [design/metrics-collection.md](design/metrics-collection.md). Establishes that
  no Tailscale "app wrapper" is needed (raw inputs come from `status --json` /
  `tailscale ping` / `tailscale metrics print`).
  Step 2 (**done**): `latency_ms` from `tailscale ping` (reuses the connector
  reachability ping; opt-in `peer-metrics --ping` for standalone use).
  Step 3 (**done**): `monitor-connectors.sh --prometheus-textfile <path>` emits an
  atomically-written node_exporter textfile of validated per-connector gauges
  (Python owns generation + the atomic write). Node-level `tailscaled_*` counters
  stay separately scrapeable via `tailscale metrics print` (a validated opt-in
  wrapper is a possible future addition). No server, no telemetry.
  *Acceptance:* `--prometheus-textfile` writes valid node_exporter textfile
  output; existing text/`--json` modes are unchanged; a test parses the emitted
  file. **(met.)**
- **Shared internal shell library.** `1.x additive-safe` (source-only). Extract
  the duplicated `read_line` / `run_root` / identity helpers into
  `scripts/lib/common.sh` incrementally, one consumer at a time, with parity
  tests — see [design/shared-shell-library.md](design/shared-shell-library.md).
  The skeleton (location + source-only guard + naming convention) has landed;
  the migration is deliberately a separate, parity-tested effort.
  *Acceptance (per design doc):* each migrated helper has a parity test, migrated
  consumers keep identical `--version`/`--help`/dry-run output, and the full
  suite stays green.

---

## Later — v2.0 (`2.0-only`)

- **Unified `ai-egress <subcommand>` CLI multiplexer.** Changes the command
  surface, so major-only.
  *Acceptance (2.0):* `ai-egress <subcommand>` dispatches to today's behaviors
  with a documented migration path; existing entrypoints remain available (or
  aliased) for at least one major cycle.
- **Remove the legacy `rollback.sh` direct-backup path** (keep plan-based
  restore). User-visible surface change.
  *Acceptance (2.0):* plan-based restore covers every prior direct-backup use
  case; the removal ships with migration notes and a deprecation cycle.
- **Windows client failover.** Needs a PowerShell path and likely different CLI
  contracts; collect demand first.
  *Acceptance (2.0):* a PowerShell controller path with parity to the
  macOS/Linux state machine; gated on demonstrated demand.

---

## Out of scope (`out of scope`)

Per [Stability](Stability.md), to keep the trust boundary small:

- A GUI.
- Provider API provisioning or automatic VPS purchasing.
- Telemetry or any project-operated server.
- Additional proxy protocols (Shadowsocks, sing-box, Xray, …).

---

## Process (any time)

- **Dependabot** for the SHA-pinned GitHub Actions.
- **Coverage ratchet** from 55% toward 70%, focused on `policy_tool` API error
  branches.
- **Bilingual doc drift check** in CI comparing the entrypoint lists in
  `README.md` and `README.zh-HK.md`.
