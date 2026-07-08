# Design: connector-failover apply mode (v1.3 flagship)

**Status:** Draft skeleton (no code; scope not yet committed)
**Tracking:** [Roadmap](../Roadmap.md) · governed by [Stability](../Stability.md)

> This is a design skeleton to anchor discussion. Sections marked **TODO** are
> deliberately unfinished; the goal of this document is to make the open
> questions explicit before any code is written.

## Motivation

Tailscale already performs native App Connector high-availability: the oldest
connector with a given tag is primary, and traffic fails over connector-to-
connector automatically (all plans). v1.1 shipped `monitor-connectors.sh`, which
**only observes** that pair and never switches anything.

The gap: if the *intended* primary connector is unhealthy in a way Tailscale's
oldest-first selection does not react to (e.g. the node is up and "oldest" but
its egress path is broken), an operator today has no audited, opt-in way to
*shift* the connector assignment. v1.3 proposes an opt-in apply mode that can
adjust the policy's connector assignment when the primary is verifiably failed —
reusing the exit-node controller's proven safety skeleton and the auditable
`plan` / `apply-plan` pipeline.

## Goals

- Opt-in only; **off by default**; observe-first (no mutation without an explicit
  `--apply`).
- Every mutation goes through the existing `plan` / `apply-plan` mechanism so it
  is auditable and restorable (`restore-plan`).
- Reuse the exit-node controller's safety properties: observe-first, hysteresis,
  cooldown, fail-closed, and post-switch readback.
- Additive under [Stability](../Stability.md): new optional flags / a new script
  only; no change to existing entrypoints' behavior or JSON schema.

## Non-goals

- No automatic VPS provisioning or purchasing (out of scope for 1.x).
- No telemetry or project-operated server.
- Not a replacement for Tailscale-native connector HA; this is a targeted
  override for the case native selection cannot resolve.

## Open question (must be resolved before implementation)

**Where does apply mode live?**

- **Option A — new `failover-connectors.sh --apply` (leaning toward this).**
  Keeps `monitor-connectors.sh`'s documented promise that it "never switches
  anything" intact, which matters for 1.x compatibility and operator trust. A
  separate apply-capable script has a clean, opt-in trust boundary.
- **Option B — extend `monitor-connectors.sh` with `--apply`.** Fewer scripts,
  but it breaks the monitor's stated read-only contract and risks surprising
  existing users/automation.

Recommendation to validate: **Option A**. TODO: confirm naming and whether the
observe path is shared code with the monitor.

## Design sketch (reuse the exit-node controller skeleton)

The exit-node controller (`failover-exit-node.sh`) already encodes the state
machine we want. Apply mode should reuse its shape:

- **Observe:** health/reachability of the primary connector (the same probes the
  monitor uses).
- **Hysteresis:** require N consecutive failed checks before proposing a switch,
  and M consecutive healthy checks before switching back.
- **Cooldown:** minimum interval between switches to avoid flapping.
- **Fail-closed:** if state is ambiguous or a precondition is unmet, do nothing.
- **Readback:** after applying, re-read the effective policy/state to confirm the
  switch took, and roll back (via `restore-plan`) if not.

The actual mutation is *not* a direct API poke: it generates a plan bundle
(connector assignment change), then `apply-plan`s it — so the change is
diffable, checksummed, and restorable exactly like a manual policy change.

TODO: define precisely what "connector assignment change" means at the policy
level (which field(s) in `tailscale.com/app-connectors` are adjusted, and how
that interacts with autoApprovers and tag ownership).

## State schema and versioning

Failover state is persisted as JSON with a `schema_version`. Apply mode adds
fields (e.g. the connector role decision, last-switch timestamp, cooldown clock).

TODO: decide whether apply mode shares `failover-state.json` with the exit-node
controller or uses its own file. If shared, bump `schema_version` and keep v1
read-compatibility (the multi-fallback item on the [Roadmap](../Roadmap.md) has
the same requirement, so coordinate the bump).

## Fail-closed decision matrix (skeleton)

| Primary health | Fallback health | Current role | Action |
|---|---|---|---|
| healthy | * | primary | none |
| failed (≥ N) | healthy | primary | propose switch → plan+apply → readback |
| failed | failed | primary | **none** (fail-closed; nowhere safe to go) |
| recovered (≥ M) | * | fallback | switch back (if `RESTORE_PRIMARY=1`) after cooldown |
| ambiguous / unreadable | * | * | **none** (fail-closed) |

TODO: fill in the remaining rows and reconcile with the exit-node controller's
existing matrix so operators only have to learn one model.

## Safety / security considerations

- Requires a policy credential (API key / OAuth) to apply; without it, apply mode
  refuses and stays observe-only.
- Every mutation is captured as a plan bundle with a diff and checksum; nothing is
  applied that an operator could not review after the fact.
- TODO: rate-limit and audit-log the switches; define what a
  `FAILOVER_NOTIFY_CMD` (see [Roadmap](../Roadmap.md)) fires on.

## Testing strategy

- Reuse the fake-`tailscale` / fake-command harness the failover tests already
  use (`tests/test_failover_scripts.py`).
- Unit-test the decision matrix directly (as the health engine's verdict is
  already tested).
- Test that without `--apply` nothing mutates, and that apply goes through
  `plan` / `apply-plan` (assert a plan bundle is produced).
- Test fail-closed paths (both failed, ambiguous state) do nothing.
- macOS CI coverage for the BSD userland paths.

## Rollout

- Ship observe-only first (already the monitor's behavior), then apply behind an
  explicit flag, documented as advanced/opt-in, defaulting off — mirroring how
  Advanced Admin Automation is gated in [Stability](../Stability.md).

## Open questions (summary)

1. New `failover-connectors.sh` vs extending the monitor (see above; leaning new).
2. Exact policy field(s) mutated for a connector reassignment.
3. Shared vs separate failover state file, and the `schema_version` bump plan.
4. Interaction with Tailscale-native oldest-first selection (avoid fighting it).
