# Design: shared internal shell library (`scripts/lib/common.sh`)

**Status:** Draft (skeleton landed; migration not started)
**Tracking:** [Roadmap](../Roadmap.md) · governed by [Stability](../Stability.md)

## Problem

`read_line` / `read_secret`, `run_root`, and connector-identity resolution are
near-duplicated across `bootstrap.sh`, `enable-exit-node.sh`, and
`restore-connector.sh` (roughly 150 lines each). The duplication is not
cosmetic: the v1.1.1 `read_line` `/dev/tty` prompt bug existed **in all three
copies**, and had to be fixed three times. The next such bug will too, unless the
helpers live in one place.

## Goal

Extract the shared helpers into a single source-only library,
`scripts/lib/common.sh`, **incrementally** — one helper and one consumer at a
time, each guarded by a parity test — so no single change has to re-verify every
entrypoint at once.

Non-goals:

- Not a CLI entrypoint. It has no `--version`, no flags, and no JSON schema, so
  it is not part of the frozen 1.x surface in [Stability](../Stability.md).
- No behavior change for any entrypoint. The extracted helper must be
  byte-for-byte behavior-compatible with the inline copy it replaces.

## Constraints

- **Source-only.** Executing the file directly must fail with a clear message
  (already enforced by the skeleton's guard).
- **Bash 3.2 + BSD userland.** Must run on stock macOS as well as Linux. This is
  exactly why a macOS CI job (see [Roadmap](../Roadmap.md)) should land before or
  alongside the migration.
- **`ai_egress_*` namespacing.** Every public helper is namespaced so a consumer
  can adopt one helper without clobbering its own inline function of the same
  name during the transition.

## Why the skeleton has no bodies yet

The three inline copies differ in subtle ways:

- `read_line` uses default-IFS `read` (whitespace-trimmed) in some scripts;
  callers depend on the trimming.
- `run_root` differs on `AI_EGRESS_USE_SUDO` / `DRY_RUN` handling and on how the
  dry-run command is echoed and quoted.
- Identity resolution reads a different set of `tailscale status` fields in
  `enable-exit-node.sh` (tag only) versus `restore-connector.sh` (tag +
  hostname), and treats ambiguity differently.

Seeding "real" bodies before a consumer uses them would either lock in one
script's semantics as canonical (silently changing the others) or create
dead code that drifts from the copies it is meant to replace. So the skeleton
reserves names and contracts only.

## Migration plan (phased, each phase its own change)

1. **Skeleton (done):** reserve `scripts/lib/common.sh`, the source-only guard,
   and the `ai_egress_*` convention. Lint + a source-only test in CI.
2. **First helper — `ai_egress_run_root`:** the smallest, most self-contained
   helper. Extract it, migrate `enable-exit-node.sh` only, and add a parity test
   (below). Leave the other two scripts on their inline copies.
3. **`ai_egress_have` / logging helpers:** trivial, low-risk; migrate all three
   consumers.
4. **`ai_egress_read_line` / `ai_egress_read_secret`:** migrate one consumer at a
   time; parity test must cover the `/dev/tty` fallback (pseudo-tty) and IFS
   trimming.
5. **`ai_egress_resolve_identity`:** last and riskiest; likely two variants or a
   parameter selecting the field set. Migrate `enable`/`restore` separately.

## Parity testing approach

For each helper, before a consumer switches to it, add a test that runs the
**extracted** helper and the **original inline** logic against the same inputs
and asserts identical stdout, stderr, and exit status. Concretely: keep a frozen
copy of the pre-migration function text in the test, source both, and diff their
behavior across a table of inputs (including edge cases: empty input, whitespace,
`--dry-run`, non-root without sudo, `/dev/tty` unavailable). The consumer only
switches once its helper's parity test is green.

## Risks

- Silent semantic drift between the three copies (mitigated by parity tests).
- macOS/Bash 3.2 regressions (mitigated by the macOS CI job).
- Scope creep — resist migrating more than one consumer per change.

## Acceptance criteria for the eventual refactor changes

- Every migrated consumer keeps identical `--version` / `--help` / dry-run output.
- The full existing suite stays green; each migrated helper has a parity test.
- `shellcheck` and `bash -n` cover `scripts/lib/common.sh`.
- No new entrypoint; `docs/Stability.md` frozen surface unchanged.
