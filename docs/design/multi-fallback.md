# Design: multi-fallback exit nodes (Phase 6)

**Status:** Design decision — under review; implementation gated on this
design merging first (same docs-first discipline as
[connector-failover-apply.md](connector-failover-apply.md)).
**Tracking:** [Roadmap](../Roadmap.md) · governed by [Stability](../Stability.md)

## The decision, in five lines

1. `FALLBACK_EXIT_NODE` accepts a **comma-separated, ordered** list of exit
   nodes; the order IS the priority (no weights, no round-robin, no
   automatic re-ordering).
2. The controller can now fail over **between fallbacks**: when the active
   fallback goes down and the primary cannot be restored, the
   highest-priority OTHER fallback that is verified this round is selected —
   today's `both_down` dead end opens up.
3. The exit-node controller's state file moves to **state schema 2**
   (`nodes.fallbacks[]`, ordered); v1 state is read-compatible; the JSON
   REPORT schemas (`probe`, `verdict`, `connectors`) stay at **1** with
   additive fields only — the state constant and the report constant split so
   the bump cannot leak beyond the Roadmap's "exit-node state alone" scope.
4. The switch safety bar is unchanged and now uniform: every node is probed
   every cycle with full hysteresis (exactly as v1.3.0 probes its two nodes),
   and a switch target must ALSO have passed its ping this round; cooldown
   blocks every switch; primary restore under `RESTORE_PRIMARY=1` always
   outranks a fallback-to-fallback move.
5. Readback becomes **identity-verified**: a fallback-to-fallback switch is
   confirmed against the concrete target node (additive
   `active-role --expect-label`), never against the role class alone — a
   switch that did not take can no longer read back as success.

## Why (scope recap)

Today `FALLBACK_EXIT_NODE` names exactly one node. If the primary AND that
fallback are both unhealthy, the controller reports `both_down` and can do
nothing, even when a third healthy exit node exists.
[Roadmap](../Roadmap.md) acceptance: an ordered fallback list is tried in
order; schema-2 state is written and pre-existing v1 state still reads; tests
cover ordered fallback and the v1 read-compat path.

## Configuration

- `FALLBACK_EXIT_NODE=node-b` — single-fallback configuration; behavior is
  pinned to v1.3.0 (see the compatibility pin below).
- `FALLBACK_EXIT_NODE=node-b,node-c,node-d` — ordered bench.
- Parsing lives in the Python health engine; the shell passes the raw string
  through unchanged. Split on `,`, trim per entry. Fail-closed validation
  BEFORE any probing: empty entry, duplicate entry, or an entry equal to
  `PRIMARY_EXIT_NODE` is a configuration error. Duplicate/primary-equality
  detection uses `_labels_equivalent` semantics as they actually are:
  exact-text for hostnames and MagicDNS labels (NOT case-insensitive), and
  canonical-address equality for IP-valued labels.
- **All-pairs live distinctness:** today's single-pair
  `_candidates_distinct` generalizes to an all-pairs check across
  primary + every fallback (node-ID equality, then IP-set intersection)
  during live-state validation — two configured labels that resolve to the
  same physical node are a live-state problem (fail closed, as today),
  because a "switch" between aliases of one node would be a lie. Resolution posture,
  pinned against the pin: an unresolved PRIMARY or unresolved ACTIVE node is
  a live-state problem (fail closed) in every configuration, as today. With
  exactly ONE configured fallback, an unresolved fallback ALSO remains a
  live-state problem — v1.3.0 fails the cycle when either candidate is
  unresolved, and the single-element semantic golden keeps that behavior
  verbatim. Only when MORE THAN ONE fallback is configured do the
  ADDITIONAL non-active bench candidates degrade instead of failing the
  cycle: an unresolved one is reported per-node and excluded from this
  round's walk (it cannot be verified, so it cannot be selected).
- **Connectors-report default guard (scoped precisely):**
  `monitor-connectors.sh` requires `FALLBACK_CONNECTOR` itself and always
  passes `--fallback`, so it never touches the nested default. Only a DIRECT
  `health_check.py connectors` invocation resolves
  `FALLBACK_CONNECTOR or FALLBACK_EXIT_NODE`; there, when
  `FALLBACK_CONNECTOR` is unset/empty AND `FALLBACK_EXIT_NODE` contains a
  comma, the default resolves to UNSET (never the first element — silently
  adopting one would misreport what the operator monitors). Documented rule:
  multi-fallback exit-node setups that also want connector monitoring set
  `FALLBACK_CONNECTOR` explicitly.

## State schema 2 (the exit-node controller's `failover-state.json` only)

```json
{
  "schema_version": 2,
  "active": {
    "role": "primary | fallback | none | unknown",
    "fallback_index": 0,
    "configured_label": "...", "node_id": "...", "tailscale_ips": [],
    "last_switch_epoch": 0.0, "last_switch_at": null
  },
  "nodes": {
    "primary":   { "configured_label": "...", "node_id": null, "tailscale_ips": [],
                   "last_state": "UNKNOWN", "fail_count": 0, "ok_count": 0,
                   "last_checked_at": null },
    "fallbacks": [ { "...same per-node shape, one per configured fallback,"
                     : "in configuration order" } ]
  }
}
```

- Constants split: `STATE_SCHEMA_VERSION = 2` governs ONLY this file;
  a new `REPORT_SCHEMA_VERSION = 1` (value unchanged from today) governs the
  `probe`/`verdict`/`connectors` JSON payloads, which gain ADDITIVE fields
  only (below). Nothing outside the exit-node state bumps.
- Per-node shape is identical to v1's (`last_state` values are the existing
  uppercase `UP`/`DOWN`/`UNKNOWN` constants); only the container changes.
- `active.fallback_index` is additive; meaningful only when
  `active.role == "fallback"`, else `null`.
- **v1 read-compat (one-way):** a well-formed v1 file is accepted;
  `nodes.fallback` seeds `fallbacks[0]` (history kept only if its stored
  label matches the FIRST configured fallback, per the existing
  label-change-resets rule); `active.role == "fallback"` seeds
  `fallback_index: 0`. The next `save_state` writes schema 2.
- **Index consistency at normalize time** (new, explicit — today's
  `normalize_state` copies `active.*` without label checks, so this is a NEW
  invariant, not a claimed existing one). Three cases, in order:
  (a) `active.configured_label` matches a configured fallback slot (by
  `_labels_equivalent`) → REBIND `fallback_index` to that slot's ordinal
  (reorders and inserts converge; identity fields kept);
  (b) the label matches NO configured slot (the delisted case) → RETAIN
  `active.configured_label` and the identity fields — they are precisely the
  evidence the `delisted` classification needs — and set `fallback_index`
  to `null` only;
  (c) type-invalid/corrupt values → the existing defensive reset.
  In every case `last_switch_epoch`/`last_switch_at` are RETAINED (the
  cooldown clock survives config edits; a config change must not unlock a
  rapid re-switch). Normalize never erases the delisted evidence — that was
  the round-2 Blocker, closed by (b).
- **Live reconciliation derives the index every cycle — persistence
  transition matrix (explicit):** `live_active_role` generalizes to return
  (role, index) by matching the live exit node's identity against primary +
  every configured fallback. NORMAL case: the live-derived role, index, and
  identity are persisted into `active.*` by the per-cycle save, exactly as
  v1.3.0's `_set_active_identity` persists every live reconciliation today —
  manual `tailscale set` selections and reordered lists converge within one
  cycle. SOLE EXCEPTION — runtime `delisted`: there is no live-derived
  configured identity to write, so the save PRESERVES the normalize-produced
  record (retained `fallback` role + retained unconfigured label +
  `fallback_index: null`) unchanged; that stored combination is the delisted
  encoding the next cycle re-derives from. `record-switch` remains the
  writer for operator-driven switches. `delisted` itself never appears in
  the state file.
- **Delisted-active recovery** (closes a trap): when the live exit node
  matches NO configured node BUT equals the state's RETAINED
  `active.configured_label`+identity (normalize case (b) above guarantees
  that record survives the delisting edit), the runtime classification is
  `delisted` rather than `unknown`: the never-override rule is about FOREIGN
  nodes, and this node is provably ours by the state's own record.
  **`delisted` is a RUNTIME-ONLY classification — an evaluate/verdict
  OVERLAY, never persisted**: returned by the generalized
  `live_active_role`, consumed by `evaluate`, and emitted as
  `active_role=delisted` in verdict text and JSON. PERSISTENCE:
  governed entirely by the transition matrix in the live-reconciliation
  bullet below — normal cycles persist the live-derived role/index/identity
  (v1.3.0 behavior); the delisted cycle preserves the RETAINED `fallback`
  role with the retained (now-unconfigured) label and
  `fallback_index: null`, and that stored combination IS the delisted
  encoding, re-derived as `delisted` by the next cycle. The persisted enum
  stays `primary|fallback|none|unknown` (schema-valid, downgrade-safe);
  `record-switch` still writes only `primary|fallback|none`; `delisted`
  never appears in the state file. From `delisted`, `evaluate` may
  restore the primary under `RESTORE_PRIMARY` — with the SAME strict bar as
  `primary_recovered`: primary reachable this round AND state UP (never the
  walk's weaker reachability-only bar); cooldown applies — reason
  `delisted_restore_primary` — or, when the primary is not restorable, walk
  the configured bench from the top (reachability-this-round, the walk's
  standard predicate; cooldown applies) — reason `delisted_next_fallback`; a cooldown block reports the existing
  `cooldown` reason; nothing selectable → none / `delisted_no_target`. All
  four are multi-capable stable codes, not single-fallback pins.
  `--ensure-primary` is NOT involved: its contract stays none-only, and
  delisted recovery is governed solely by the rows above. A live node the
  state does NOT claim stays `unknown_active` / never overridden, unchanged.
- **Downgrade runbook (documented in Configuration/Failover):** an older
  build reading schema-2 state defensively resets it, and with a comma list
  still configured it treats the whole list as one unresolvable hostname and
  stops evaluating (fail-closed but stuck). Downgrade procedure: stop the
  watcher → set a scalar `FALLBACK_EXIT_NODE` → accept (or archive) the
  state reset → restart. Same corrupt-state-class cost as today, now written
  down.

## Probing and hysteresis (uniform; replaces the draft's lazy proposal)

**Every configured node — primary and ALL fallbacks — is probed every verdict
cycle, with hysteresis counters applied to each,** exactly generalizing what
v1.3.0 does for its two nodes. Rationale over lazy candidate probing: the
single-element path stays behaviorally identical to v1.3.0 (same ping calls,
same counters, same `last_checked_at`, same probe JSON — the compatibility
pin below becomes testable); reasons classify from full knowledge (DOWN vs
unverified distinctions keep today's threshold-state derivation); and the
bench is warm when a walk happens. Cost is N pings per cycle, each bounded by
`PING_TIMEOUT` and run in configuration order; the docs state this cost
plainly (a long bench lengthens the cycle; `CHECK_INTERVAL` paces it).
`--egress` probing stays active-node-only, as today.

## Decision semantics (extends `evaluate`; reason strings pinned)

Roles stay `primary | fallback | none | unknown` (+ the `delisted` reporting
state above). `Decision` gains additive `target_index`; single-fallback
configurations keep TODAY'S reason strings verbatim (`both_down`,
`fallback_unverified`, `fallback_down`, …) — the new multi-only reasons
appear only when more than one fallback is configured.

| Active | Condition | Action / reason |
|---|---|---|
| primary | primary not DOWN | none / `healthy` (unchanged; a primary whose state is UP but whose ping failed THIS round follows today's hysteresis exactly — reachability feeds the counters, `last_state` flips only at thresholds) |
| primary | primary DOWN → walk fallbacks in order; select the FIRST whose ping passed THIS round (`probe.reachable` — exactly v1.3.0's bar for its sole fallback: today's switch row checks `fallback_probe.reachable` ONLY and does not consult the target's `last_state`, so a mid-hysteresis UNKNOWN or even DOWN-state node with a passing ping this round is selectable, exactly as today) | switch-to-fallback(index) / `primary_down`; cooldown blocks (unchanged) |
| primary | primary DOWN, no fallback selectable | none / single-fallback: today's `both_down` vs `fallback_unverified` split verbatim; multi: `all_fallbacks_down` when every bench node's state is DOWN, else `no_fallback_verified`. Wording split, pinned: a candidate whose state is UNKNOWN but whose ping PASSED this round is SELECTABLE (the predicate is reachability, per the row above); `no_fallback_verified` covers only candidates whose ping failed/errored or could not run this round — a failed or errored ping is "not verified", the walk continues past it, and declared priority bends ONLY toward safety, never toward an unverified earlier candidate |
| fallback[i] | primary reachable + state UP + `RESTORE_PRIMARY=1` | switch-to-primary / `primary_recovered` (cooldown applies). SAME-CYCLE TIE-BREAK, explicit: when this row fires, the next-fallback walk is NOT evaluated that cycle — restore outranks it by construction |
| fallback[i] | fallback[i] DOWN, primary not restorable this cycle (down, unverified, or `RESTORE_PRIMARY=0`) → walk the other fallbacks from the top (j ≠ i, order = priority, not round-robin); first verified-this-round (`probe.reachable`, same predicate as above) | switch-to-fallback(j) / `fallback_down_next_fallback` — **the new capability**; cooldown blocks. NOTE: with `RESTORE_PRIMARY=0` and a healthy primary, the walk still runs — restore is disabled, surviving is not |
| fallback[i] | fallback[i] DOWN, nothing selectable | none / single-fallback: today's `both_down`/`fallback_down` verbatim; multi: `all_down` only when primary AND every bench node are DOWN, else `no_fallback_verified` |
| fallback[i] | fallback[i] healthy, primary not UP (or `RESTORE_PRIMARY=0` with event `primary_recovered`, unchanged) | none / `staying_on_fallback` / `restore_primary_disabled` (unchanged) |
| none | `--ensure-primary` + primary reachable | switch-to-primary (unchanged; ensure-primary NEVER selects a fallback) |
| unknown / delisted | see the recovery bullet above | `unknown_active`: none, never overridden. `delisted`: restore or walk permitted, loud reason |

## Report JSON (additive; report schema stays 1)

- `verdict --json`: keeps `schema_version: 1` (now `REPORT_SCHEMA_VERSION`),
  keeps the top-level `primary` and `fallback` probe objects — `fallback`
  is pinned to `fallbacks[0]`'s probe for consumer continuity — and ADDS
  `fallbacks: [ {probe…}, … ]` (all bench probes, in order) plus
  `decision.target_index`. `Decision.fallback_state` (scalar) keeps meaning
  `fallbacks[0]`'s state; an additive `fallback_states` array carries all.
  Text output appends `target_index=<n>` only for fallback targets.
- `probe` / `connectors` payloads: untouched except the connectors default
  guard above.

## Surfaces that change (all additive)

- `health_check.py`: list parsing + validation; probe/hysteresis loop over
  the bench; `evaluate` walk + pinned reasons; state schema 2 + normalize
  invariants + live index reconciliation + `delisted`; constants split;
  `active-role` gains `--expect-label <label>` (exit 0 only when the live
  exit node's identity matches that concrete label — the canonical-IP-aware
  comparison — additive; the existing role-printing mode is unchanged);
  `record-switch` gains `--fallback-index <n>` AND `--label <label>` (the
  concrete node label the shell just switched to and readback-verified) with
  fail-closed validation: the index must be in range, and the configured
  list's slot at that index must be `_labels_equivalent` to the passed
  `--label` — a mismatch (the shell's view of the bench diverging from the
  engine's) refuses with nothing written. Both flags are required for
  fallback-role records when more than one fallback is configured;
  single-element lists default the index to 0 and validate `--label` when
  given. This makes the mismatch case representable and testable (the
  round-3 gap).
- `failover-exit-node.sh`: parses `target_index` (a fallback target with a
  missing/malformed index is treated as a failed verdict — skip the cycle
  loudly, no switch); `record_switch` passes BOTH `--fallback-index
  "$target_index"` AND `--label "$target_label"` (the same readback-verified
  label) for fallback records, satisfying the engine's fail-closed pairing
  (index+label required together on multi configs); **readback is identity-verified**: after
  `tailscale set`, confirm via `active-role --expect-label "$target_label"`
  (not the role class) — `target_label` comes from the verdict's own
  `target_label` field and is cross-checked against the configured list
  entry at `target_index` (same `_labels_equivalent`/canonical-IP rules as
  `--expect-label` itself; a mismatch between the two is a failed verdict,
  no switch), so a fallback-to-fallback switch that did not take
  reads back as FAILURE (no state record, no cooldown restart via
  record-switch, `FAILOVER_EVENT=failed`) — with a dedicated no-op-switch
  test; `record-switch` call passes `--fallback-index` + `--label` as above. Notify contract:
  `FAILOVER_ROLE`/`FAILOVER_LABEL` unchanged, additive
  `FAILOVER_FALLBACK_INDEX` (empty for primary targets; `0` for a
  single-element list's fallback — an intentional additive-safe extension
  the compatibility pin explicitly allows: role/label values stay
  identical, the new variable is merely additional).
- Operator docs (`Configuration.md`, `Failover.md`,
  `examples/failover.env.example`): list syntax, order-is-priority,
  probing-cost note, reorder-resets-history, the delisted recovery, the
  downgrade runbook, the connectors-default rule.
- NOT changed: `monitor-connectors.sh`, `failover-connectors.sh`, the
  connector-switch state file, `policy_tool.py`, every report schema.

## Compatibility pin (behavioral, not byte; steady-state scope)

With a SINGLE-element `FALLBACK_EXIT_NODE`, v1.3.0 behavior is preserved as
a semantic golden **for steady-state, same-configuration sequences**: same
probe calls (both nodes every cycle), same hysteresis transitions, same
decisions and reason strings, same notify role/label values, same JSON
report keys with `fallback` still populated — state file fields identical
MODULO the documented additives (container rename to `fallbacks[]`,
`fallback_index`, `schema_version: 2`). EXPLICIT CARVE-OUT: configuration-
edit transitions are OUTSIDE the pin — replacing the sole fallback while
Tailscale still sits on the old node classifies `delisted` and may recover
(restore or walk), where v1.3.0 dead-ended on `unknown_active`; that
deliberate improvement is exactly what the delisted tests assert, and the
golden gate covers only unchanged-config sequences so the two suites cannot
fight. It is explicitly NOT a state-file byte pin; the implementation gate
is a fixture-driven equivalence test against v1.3.0-recorded decision
sequences under fixed configuration.

## Non-goals

No weights/health-scores/auto-reorder; no round-robin memory beyond
`active.fallback_index`; no connector-switch multi-pool (its own future
design, per the Phase 5 decision); no change to probe transports, threshold
semantics, cooldown semantics, or the `--ensure-primary` contract; no report
schema bump.

## Testing strategy (implementation-phase gate)

Pure-`evaluate` unit rows for EVERY table row incl. the multi-only reasons,
first-in-order-wins with multiple verified candidates, walk-continues-past
unverified/errored candidates, restore-outranks-walk same-cycle,
`RESTORE_PRIMARY=0` walk row, ensure-primary-never-fallback; single-element
semantic-golden equivalence vs v1.3.0-recorded sequences; v1→v2 read-compat
(history kept/reset by label; index seeded; cooldown clock survives) + the
normalize index-consistency invariants (out-of-range, label-mismatch,
retained switch clock); live index reconciliation (manual selection,
reorder, delisted-active recovery both arms — restore and walk — and the
foreign-node never-override contrast); all-pairs distinctness refusals +
unresolved-bench-candidate exclusion; list validation refusals before
probing; identity-verified readback incl. the fallback-to-fallback NO-OP
switch case (reads back failure, no record, event=failed);
`record-switch --fallback-index` bounds/mismatch refusals; verdict JSON
additive shape (legacy keys pinned to fallbacks[0]); connectors guard
(direct-invocation scope, unset-vs-empty distinguished, adopt-first
forbidden); notify `FAILOVER_FALLBACK_INDEX`; plus a falsification corpus in
the established style (walk order inverted; unverified candidate accepted;
role-class readback restored; v1 state rejected; report schema bumped;
cooldown clock dropped on config edit; guard removed — each RED on a named
discriminator).

## Rollout

1. This design PR (docs only; dual-reviewed to PROCEED before merge).
2. ONE implementation PR: `health_check.py` + `failover-exit-node.sh` +
   tests + operator docs + CHANGELOG `[Unreleased]` — the Python protocol,
   shell consumer, state migration, and docs must land in lockstep.
3. Ships in the next minor (v1.4.0); `1.x additive-safe`.
