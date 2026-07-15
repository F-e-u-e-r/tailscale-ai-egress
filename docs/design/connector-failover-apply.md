# Design: connector-failover apply mode (v1.3 flagship)

**Status:** Design decision — scope committed (2026-07-16); implementation gated
on the [Pre-implementation gate](#pre-implementation-gate)
**Tracking:** [Roadmap](../Roadmap.md) · governed by [Stability](../Stability.md)

## The decision, in five lines

1. **Same-tag native HA stays the default and recommended deployment model.**
   Nothing about it changes; the monitor stays read-only, permanently.
2. Apply mode ships as a separate **advanced** mode: the **distinct-tag active
   connector switch** — an operator-invoked, auditable, policy-level *forced
   selection* of which connector pool (tag) serves the AI domain set.
3. It exists for **forced selection when the primary is online-but-bad** — wrong
   egress IP, degraded provider path, provider quota/rate-limit exhaustion, or
   manual evacuation — conditions Tailscale's native oldest-first selection
   cannot see, because the node itself looks healthy.
4. It is **not a replacement for Tailscale-native HA**; the two compose. Within
   each pool (tag), native HA keeps working exactly as today; the switch moves
   the domain set *between* pools.
5. **No implementation before the gate:** the operator-facing docs — deployment
   models, migration path (both directions), rollback story, warnings — must be
   MERGED before implementation work opens (see
   [Pre-implementation gate](#pre-implementation-gate)).

## Why the scope was decided this way (mechanism finding)

The original skeleton left "what field does the switch actually change?" as a
TODO. Investigation closed it:

- The only connector lever in the policy *file* is the app-connector entry's
  `connectors` tag list (`tailscale.com/app-connectors` → `"connectors":
  ["tag:ai-egress-jp"]`). Multiple nodes sharing that tag form the native HA
  pool, and Tailscale picks **oldest-first** among them by node registration
  order — **no policy-file field reorders that selection within one tag.**
- Therefore a `plan`/`apply-plan` (policy-file) switch — the Roadmap's
  auditability requirement — is only *meaningful across distinct tags*. A
  policy-based "switch" inside one tag does not exist.
- A devices-API node-tag mutation could force selection inside one tag, but it
  bypasses the plan bundle pipeline (not diffable/checksummed/restorable), so it
  was rejected (see Non-goals).

Hence the two-model split: keep the same-tag world untouched and default, and
offer the distinct-tag switch as an explicitly separate, opt-in, advanced mode.

## The two deployment models

### Model A — one tag, native HA (default, recommended, unchanged)

All connectors share one tag (e.g. `tag:ai-egress-jp`). Tailscale handles
failover natively; `monitor-connectors.sh` observes and never mutates. If you do
not have a reason to force which pool egresses your AI traffic, use Model A and
stop reading.

### Model B — distinct-tag pools + forced selection (advanced, opt-in)

Primary and fallback connectors are deployed under **distinct tags** (e.g.
`tag:ai-egress-jp` and `tag:ai-egress-sg`). The app-connector entry's
`connectors` list names the **active** pool; the switch rewrites that list via
the plan-bundle pipeline. Each pool may still contain several nodes — native HA
keeps operating *inside* the active pool. Model B is strictly additive on top of
Model A mechanics, and switching back is the same operation in reverse.

**Pool-pair source of truth:** the pair is *declared configuration*, not
inference — additive `failover.env` keys `PRIMARY_CONNECTOR_TAG` /
`FALLBACK_CONNECTOR_TAG` name the two pools (mirroring how the monitor names
its two nodes). Which pool is *active* is read from the live policy, never
assumed: before the first switch the policy's one-element `connectors` list
already identifies it.

**Model B configuration invariants (enforced, fail-closed):**
- Any switch requires BOTH keys: present, valid `tag:` syntax, and distinct;
  otherwise refuse with a configuration pointer.
- `--to` must name one of the configured pair — the tool never selects a tag
  outside it (no arbitrary third-tag switches through the supported path).
- The live `connectors` value must be exactly one element AND within the pair
  for the SCRIPTED path. Empty, multi-tag, or out-of-pair values are **drift**:
  report shows the verbatim value; the script refuses to switch until it is
  reconciled. Documented recovery: operator review, then the raw
  `connector-plan --switch-to tag:<pool>` escape hatch — the planner accepts
  any current shape and its bundle diff IS the reconciliation (verbatim `from`
  array → one-element `to`) — or a console edit.
- **Node-membership disjointness is an operator requirement, surfaced not
  assumed:** report (and the switch path) print a prominent warning listing
  any node that carries BOTH pool tags — such a node stays active in either
  pool, so a switch cannot evacuate it. The switch itself is not refused
  (hard refusal would deadlock legitimate transitional retagging), but the
  warning names the nodes and the docs state the requirement.

## What operators use it for (and what not)

| Situation | Native HA reacts? | Model B switch helps? |
|---|---|---|
| Node offline / unreachable / route withdrawn | **yes** | not needed |
| Egress IP wrong (geo/reputation/provider NAT change) | no — node looks healthy | **yes** |
| Provider path degraded (loss/latency, peering incident) | no | **yes** |
| Provider quota / rate-limit exhausted for the AI service | no | **yes** |
| Planned maintenance / deliberate evacuation | no | **yes** |
| Automatic, unattended failover on health signals | native HA already does the node-down case | **no — out of scope in v1.3** (see Non-goals) |

## Mechanism (v1.3)

- **New script `failover-connectors.sh`** (Option A from the skeleton,
  confirmed): `monitor-connectors.sh`'s documented "never switches anything"
  promise stays intact, permanently.
- **Operator-invoked, one-shot, observe-first:**
  - `failover-connectors.sh` (no args) — report Model B state: the managed
    app-connector entry and its current `connectors` value (live policy),
    declaration checks for both pool tags, both pools' node liveness (from
    `tailscale status --json` peer tags), state-file drift. Mutates nothing,
    writes nothing. **Without a policy credential** the policy-derived fields
    (active pool, declarations, drift) are reported `unavailable`; the
    status-derived liveness fields still work.
  - `failover-connectors.sh --to tag:<pool>` — run the fail-closed
    precondition checks (advisory pre-flight for good errors), then generate an
    auditable **plan bundle** via the connector-scoped `connector-plan
    --switch-to` (below) and print its diff. **The planner re-enforces every
    policy-derived precondition against the snapshot it fetches and bundles**
    — the authoritative check — so a policy edit slipping in between the
    script's pre-flight and the planner's fetch cannot produce a bundle that
    violates readiness under a fresh ETag (TOCTOU-closed; tested).
    **Stops there**: the tailnet is not written; the bundle sits under
    `generated/policy-plans/` for review.
  - `failover-connectors.sh --to tag:<pool> --apply` — additionally
    **re-check the volatile preconditions** (target pool still has an online
    tagged node) immediately before `policy_tool.py apply-plan <bundle>`, which
    keeps its existing exact confirmation (`APPLY <plan-id>`) and
    If-Match/ETag conflict detection. Then **readback**: re-fetch the policy
    and verify the `connectors` list equals the target. On mismatch, re-read
    once (propagation lag); if still mismatched, **nothing further is written
    automatically** — report loudly, print the two recovery paths (a
    compensating switch-back plan, or `restore-plan` with its
    whole-policy-overwrite caveat), and exit non-zero.
  - Switching back is the same command pointed at the primary tag.
- **`policy_tool.py` gains a connector-scoped planning mode (additive):** today
  `merge_policy` is add-only — `connectors` is merged with `ordered_union`
  (`scripts/policy_tool.py:556`), which cannot *remove* the old tag — and the
  ordinary `plan` path always performs the FULL additive merge (it can create
  the managed entry and add owners/approvers/grants/domains). A switch must not
  ride on that. v1.3 adds one scoped planning subcommand (working name
  `connector-plan`) with two mutually exclusive operations, reusing the exact
  same fetch → ETag → validate → bundle → manifest pipeline but mutating ONLY
  an allowlisted slice of the FETCHED policy — no full merge, ever:
  - `connector-plan --switch-to tag:<pool>` — replaces the managed entry's
    `connectors` list with exactly **one element**. The arity constraint is on
    the ARGUMENT: exactly one target tag (an empty or multi-tag `--switch-to`
    value is rejected — a multi-tag "set" would be dual-active by design, not
    a switch). The CURRENT live value may be any shape — empty, multi-tag, or
    outside the configured pair — and the planner always emits the exact
    one-element replacement, which is precisely how an abnormal dual-active or
    out-of-pair state gets reconciled through the audited pipeline; the
    "already active" refusal applies only when the current value is exactly
    `[target]`. `manifest.json` records `from` as the VERBATIM previous array
    (aligning with the state file's `previous_connectors`).
    An optional **`--expected-from <verbatim-list>`** makes the planner refuse
    unless its fetched snapshot's `connectors` equals that exact value — a
    compare-and-swap at plan time. **The scripted path always passes it** (the
    value it just validated), so a shape change between the script's drift
    check and the planner's fetch is refused rather than silently reconciled —
    the scripted path's drift-refusal promise is enforced by the binding
    check, not just advisorily. The raw reconciliation path may omit it. The planner
    additionally enforces the policy-derived preconditions — exactly ONE
    app-connectors entry matching `--connector-name` (zero or duplicate
    matches → refuse), target declared in `tagOwners`, autoApprovers +
    DNS-grant readiness — against its OWN fetched snapshot (the
    `current.hujson`/ETag the bundle captures), refusing to emit otherwise;
    this, not the script pre-flight, is the binding check. The bundle diff is
    remove+add on that one array and nothing else; the planner refuses to emit
    a bundle containing any other semantic change (that allowlist check is the
    union-regression tripwire).
  - `connector-plan --declare tag:<pool>` — Model B setup: adds `tagOwners`,
    both `autoApprovers.routes` entries (`0.0.0.0/0`, `::/0`), and the
    member→pool DNS grant (`tcp:53`/`udp:53`) for the named tag, touching
    NOTHING else — notably not `connectors` — so declaring the fallback pool
    changes no routing.
  - Bundles record the operation in `manifest.json` (`operation:
    "switch-connectors"` with `from`/`to` tags; `operation: "declare-pool"`
    with `to` only), and `list-plans` — text AND `--json` — surfaces those
    fields so audits identify a switch at a glance (today it ignores unknown
    manifest keys, `scripts/policy_tool.py:1582`, so this is an explicit
    implementation requirement, not a free property). Legacy manifests without
    `operation` keep rendering exactly as before (compat test);
    `apply-plan` / `restore-plan` consume these bundles unchanged.
  - When these operations are not invoked, `plan`/`merge` behavior and every
    schema stay byte-identical to today (a Stability regression test pins
    this).
  - **Expert escape hatch, documented as such:** driving `connector-plan` +
    `apply-plan` directly bypasses `failover-connectors.sh`'s preconditions
    and apply-time liveness recheck. The supported operator path is the
    script; the raw subcommand remains available to experts without
    precondition blocking.
  - **Model B source-of-truth rule:** after a switch, an ordinary full-merge
    `plan` whose inputs still name the old primary tag would re-union it into
    `connectors` (dual-active) and silently undo the switch. Operator docs
    therefore forbid routine full-merge plans against the managed entry in
    Model B unless `--connector-tag` names the currently active pool; a test
    documents the dual-active failure mode.
- **Fail-closed preconditions** (all checked before a bundle is generated; any
  failure refuses with a specific pointer):
  1. A policy credential is present (else the switch refuses; report still
     works in its degraded, status-only form).
  2. The managed app-connector entry exists and matches `--connector-name`
     (default `AI-Egress-<REGION>`) exactly once.
  3. The target tag is declared in `tagOwners`.
  4. `autoApprovers.routes` for `0.0.0.0/0` and `::/0` include the target tag,
     and the member→target DNS grant (`tcp:53`/`udp:53`) exists — the full
     pool-readiness invariant, everything `--declare` establishes.
  5. The target pool has ≥ 1 **online** node carrying the tag in
     `tailscale status --json` (re-checked at apply time; see above).
  6. Current policy fetch + ETag succeed.
  Preconditions 3–4 are established once, at Model B setup time (via
  `--declare`). Precondition 5 is dynamic status: it is verified whenever a
  switch is planned and re-checked immediately before apply. With readiness
  already in place, an incident-time switch bundle is a single small,
  reviewable `connectors` diff and nothing else.
- **State (additive, own file):** `generated/connector-switch-state.json`
  (`schema_version: 1` of its own): `active_tag`, `previous_connectors` (the
  verbatim pre-switch list — an array, so an abnormal multi-tag pre-switch
  state is recorded faithfully rather than forced into a scalar),
  `last_switch_at`, `last_plan_id`. Deliberately **not** shared with the
  exit-node controller's `failover-state.json` — no bump of that schema, no v1
  read-compat risk, and the Roadmap's multi-fallback item (`schema_version: 2`)
  stays scoped to the exit-node controller alone. The two features decouple.
  - **Lifecycle:** the file is written **only after** a successful `apply-plan`
    **and** a successful readback (never on plan-only runs, never on a
    readback mismatch — it keeps the last verified reality).
    `previous_connectors` is captured from the live pre-switch policy at
    switch time, not from older state. Writes are atomic (temp file + rename) under the tool's lock. Cold
    start (no file) is valid: report mode derives the active pool from the
    policy.
  - **Policy is the source of truth:** report mode always reads the live
    policy; when the state file disagrees (console edit, or a bare
    `policy_tool.py restore-plan` run outside the script), it reports **drift**
    and treats `previous_connectors`/timestamps as advisory until the next
    successful scripted switch. A bare `restore-plan` intentionally does not
    touch this file.
- **Cooldown (anti fat-finger, not anti-flap):** if the last switch is younger
  than `CONNECTOR_SWITCH_COOLDOWN` (default e.g. 600s), print a prominent
  warning including the previous switch's timestamp and plan id; the existing
  `APPLY <plan-id>` confirmation then still stands between the operator and the
  write. No hard refusal (a manual evacuation must not be lockable-out by its
  own tool), no `--force` flag to grow into a bypass habit. An already-active
  no-op does not count as a switch and does not touch state or the cooldown
  clock.
- **Notify hook:** on a completed switch (and on a failed readback), fire the
  existing `FAILOVER_NOTIFY_CMD` contract with its established env fields
  (`FAILOVER_EVENT=connector-switch|connector-switch-readback-failed`,
  `FAILOVER_ROLE`/`FAILOVER_LABEL` carrying the pool tags, `FAILOVER_REASON`,
  plus an additive `FAILOVER_PLAN_ID`). Absence of the hook is a no-op; hook
  failure never changes the switch outcome (same contract as today,
  `failover-exit-node.sh` `notify_hook`).

## Fail-closed decision matrix (forced selection)

| Condition at invocation | Action |
|---|---|
| No `--to` | Report state; mutate nothing (observe-first). |
| `--to` without `--apply` | Preconditions → plan bundle + diff only; tailnet untouched. |
| `--to` names the pool that is already active | Refuse before plan generation ("already active"); no bundle, no state/cooldown change. |
| Pool-pair keys missing / invalid / not distinct, or `--to` outside the pair | Refuse with a configuration pointer. |
| Live `connectors` is empty, not exactly one element, or outside the configured pair (drift) | Report the verbatim value; refuse the scripted switch (recovery: reviewed raw `connector-plan --switch-to`, which accepts any current shape and emits the exact one-element reconciliation, or a console edit). |
| A node carries BOTH pool tags | Prominent warning naming the node(s) — a switch cannot evacuate it; not a refusal (transitional retagging must stay possible). |
| Policy edited between the script pre-flight and the planner's fetch | The planner's own snapshot checks refuse the bundle; the scripted path also passes `--expected-from`, so even a shape change that a bare reconciliation would accept is refused (TOCTOU-closed). |
| Credential missing | Refuse the switch (degraded status-only report still works). |
| Managed entry missing, or duplicate `--connector-name` matches | Refuse (ambiguous target). |
| Target tag not in `tagOwners`, or `autoApprovers`/DNS-grant readiness missing | Refuse, point at `--declare` + Model B setup docs. |
| Target pool has zero online tagged nodes (plan time or apply-time recheck) | Refuse (nowhere safe to go — same posture as the exit-node matrix's failed/failed row). |
| Status/policy unreadable or ambiguous | Refuse (fail-closed). |
| ETag conflict at `apply-plan` | Existing 412 behavior: instruct to regenerate the plan; nothing written. |
| Cooldown window active | Warn loudly; existing exact confirmation still required; proceed only on it. |
| Readback mismatch after apply | Re-read once; if still mismatched: **no automatic second write** — report + print recovery commands (compensating switch-back plan; `restore-plan` with overwrite caveat); exit non-zero. |

## Non-goals (v1.3)

- **No health-driven automatic watcher.** The trigger set this mode exists for
  (wrong egress IP, degraded provider path, quota) is *not detectable* by the
  current health engine, and auto-switching on the signals it *does* have would
  re-implement — and fight — Tailscale-native HA, which point 4 of the decision
  forbids. If future metrics (e.g. egress-IP probes, latency trends from
  `peer-metrics`) make online-but-bad detectable, an automated mode can be
  *proposed as its own design*, gated the same way. Nothing in this design
  precludes it; nothing in v1.3 ships it.
- **No devices-API node-tag mutation** (rejected Option B): not expressible as
  an auditable/restorable plan bundle.
- **No change to `monitor-connectors.sh`** beyond, at most, documentation
  cross-references. Its read-only promise is permanent.
- No provisioning, no telemetry, no new long-running daemons
  ([Stability](../Stability.md) boundaries).

## Migration path (gate content, summarized here; full operator steps ship in docs BEFORE implementation)

**Model A → Model B (order matters; the declare and configuration steps are
routing no-ops — provisioning via retag is the one exception, flagged below):**
1. Declare the fallback pool in policy FIRST:
   `connector-plan --declare tag:<fallback>` → review the bundle →
   `apply-plan`. This adds `tagOwners` + `autoApprovers` + the DNS grant for
   the fallback tag only; the app-connector `connectors` list still names only
   the primary, so **routing is unchanged** — and node registration under the
   new tag becomes possible (Tailscale rejects `--advertise-tags` for an
   undeclared/unowned tag).
2. Provision the fallback pool, one of two documented paths:
   - **New node (preferred):** bootstrap this repo on it with the fallback
     tag — the bootstrap already runs `tailscale up --advertise-connector
     --advertise-tags=...` (an app connector needs BOTH flags).
   - **Retag an existing node — a maintenance-window operation, NOT a routing
     no-op:** moving a node out of the ACTIVE pool changes that pool's
     membership immediately — native selection fails over inside the pool
     (brief disruption is possible), and if it was the pool's ONLY online node
     this is an outage until the move completes. **Verify the primary pool
     keeps ≥ 1 other online node first**; prefer the new-node path for
     zero-impact adoption. For the mechanics, prefer the **Admin Console** —
     the CLI path (`tailscale up`) requires restating every non-default flag
     (see `docs/Troubleshooting.md` § "`tailscale up` Complains About
     Non-Default Flags") and auth-key-tagged nodes carry additional retagging
     restrictions.
3. Set `PRIMARY_CONNECTOR_TAG` / `FALLBACK_CONNECTOR_TAG` in `failover.env`.
4. Verify: `failover-connectors.sh` (no args) reports both pools declared +
   green and preconditions met. With the new-node path, traffic behavior until
   a switch is applied is byte-for-byte what Model A produced; the retag
   path's pool-membership change (step 2) is the one exception.
5. Ongoing: routine full-merge plans in Model B must follow the
   source-of-truth rule (see Mechanism) so they cannot re-union the inactive
   pool back in.

**Model B → Model A:**
1. If the fallback is active, switch back to the primary pool
   (`--to tag:<primary> --apply`).
2. Retire or retag the fallback nodes.
3. Remove `PRIMARY_CONNECTOR_TAG` / `FALLBACK_CONNECTOR_TAG` from
   `failover.env` (and optionally delete
   `generated/connector-switch-state.json`).
4. Optional declaration cleanup: removing the fallback tag's `tagOwners` /
   `autoApprovers` / DNS-grant entries is **not expressible in the 1.x
   add-only pipeline** — it is a documented manual Admin Console edit. Leaving
   the declarations in place is explicitly safe (a declared tag with no nodes
   and no `connectors` reference routes nothing); the docs say so.

## Rollback story (gate content)

Three layers, preferred first:
1. **Compensating switch-back** — `failover-connectors.sh --to tag:<previous>`
   (or raw `connector-plan --switch-to`): connector-only by construction, so it
   is safe under concurrent policy edits; the state file remembers
   `previous_connectors` and report mode shows the live value.
2. `policy_tool.py restore-plan <bundle>` — restores the exact pre-switch
   policy captured in the bundle (existing, tested machinery; requires
   `RESTORE <plan-id>`). **Caveat, stated wherever it is offered:** it rewrites
   the WHOLE policy, so any legitimate policy edit made after the switch would
   be overwritten — use it only when the policy has not otherwise changed
   since the switch.
3. Documented manual fallback: edit the `connectors` list in the Admin Console
   (the diff is one array).

## Warnings (gate content; must ship verbatim-equivalent in operator docs)

- The switch **overrides** which pool Tailscale would natively serve; you own
  the consequences of pinning traffic to the fallback pool (different egress
  IPs/geo/reputation, provider cost, latency).
- Model B requires disciplined tag hygiene: both tags must stay owned,
  auto-approved, and DNS-granted, or a switch will strand routes or DNS (the
  preconditions catch this, but only at switch time — set-up drift is on the
  operator).
- **Post-switch merge hazard:** the ordinary merge path is add-only by design;
  a routine re-plan that still lists the old primary pool re-unions it into
  `connectors` (dual-active) and silently undoes the switch. After a switch,
  connectors-touching plans must name only the intended active pool or use the
  connector-scoped mode (see Mechanism).
- Concurrent hand-edits of the policy race the switch; `apply-plan`'s If-Match
  detects and refuses (regenerate the plan). For rollback under concurrency,
  prefer the compensating switch-back; `restore-plan` rewrites the whole
  policy (see Rollback).
- Policy readback confirms the *policy* changed, not that every client's routes
  have converged; allow for route propagation (typically seconds to a couple of
  minutes) before judging the switch by client behavior.
- In connector-only mode with no exit node, a switch mid-flow breaks existing
  connections; new connections move after route propagation (same caveat as
  native failover, worth restating here).
- The state file records what *this tool* did; out-of-band changes (console
  edits, bare `restore-plan`) are detected at report time by comparing policy
  reality, not assumed.

## Pre-implementation gate

Binding order (owner point 5): **items 1–5 are operator-facing documentation
and must be MERGED before any implementation PR opens.** Item 6 is done by this
design PR; item 7 binds the implementation plan itself.

1. `docs/Failover.md` advanced-mode section: both deployment models, the
   when-to-use table, and the explicit "does not replace native HA" statement.
2. `docs/Configuration.md`: every new knob (`PRIMARY_CONNECTOR_TAG`,
   `FALLBACK_CONNECTOR_TAG`, `CONNECTOR_SWITCH_COOLDOWN`, state file location,
   `--connector-name` interplay).
3. The migration path above, both directions, as executable operator steps
   (declare → retag → verify; switch-back → retire → manual cleanup note).
4. The rollback story above, all three layers with the `restore-plan`
   overwrite caveat.
5. The warnings above.
6. `docs/Roadmap.md` acceptance updated to this design (this PR).
7. The implementation plan enumerates tests for: no-`--apply` never mutates;
   `--apply` produces + applies a bundle (mocked API) and passes readback;
   every fail-closed matrix row (including already-active no-op, duplicate
   managed entries, missing ETag, apply-time liveness recheck, stale-plan 412,
   pool-pair configuration invariants, out-of-pair/multi-tag drift refusal,
   dual-tagged-node warning); a policy edit between the script pre-flight and
   the planner's fetch is refused by the planner's snapshot checks, including
   a shape change caught by the scripted path's `--expected-from`
   compare-and-swap (TOCTOU);
   readback-mismatch writes nothing further and exits non-zero; cooldown
   warning; `--switch-to` exact one-element replace with a diff-allowlist
   check (diff is remove+add on the one array and nothing else); `--declare`
   touches only the three declaration surfaces; the subcommand-absent path
   stays byte-identical to today's plan/merge output (Stability regression);
   the documented dual-active failure mode (ordinary full-merge re-union after
   a switch) exercised as a test; the planner accepts an empty/multi-tag/
   out-of-pair CURRENT value and emits the exact one-element reconciliation
   (with verbatim `from` in the manifest) while the scripted path refuses that
   same drift, and the already-active refusal fires only when current ==
   `[target]`; `list-plans` surfaces `operation`/`from`/`to`
   in text and `--json` while legacy manifests render unchanged; state-file
   lifecycle (written only after apply+readback; drift reporting; atomic
   write); macOS (BSD) leg.

## Testing strategy (implementation phase)

Reuse the established harness: fake `tailscale` status fixtures for pool
liveness (peer tags); the mocked Tailscale API for
declare/switch/apply-plan/readback; bundle assertions (manifest `operation`,
diff allowlist); fail-closed rows as table-driven tests; state-file schema
round-trip + drift; the macOS CI leg for BSD userland; mutation-style
assertions (union-instead-of-replace, add-only diffs, and multi-tag values must
fail the exact-diff tests).

## Rollout

1. This design-decision PR (docs only: this file + Roadmap acceptance).
2. Operator-docs PR (gate items 1–5; no behavior change). **Merges before any
   implementation PR opens.**
3. Implementation PR(s): `policy_tool.py connector-plan` + tests →
   `failover-connectors.sh` + tests.
4. CHANGELOG under `[Unreleased]` when behavior ships; v1.3 release notes name
   Model B explicitly as advanced/opt-in.

## Resolved questions (was: Open questions)

1. **New script vs extending the monitor** → new `failover-connectors.sh`; the
   monitor's read-only promise is permanent.
2. **Exact policy mutation** → a connector-scoped planning subcommand
   (`connector-plan`) with two allowlisted operations: `--switch-to`
   (exact-replace of the managed entry's `connectors` with a **one-element**
   list; refuses any other semantic diff) and `--declare` (pool readiness:
   tagOwners + autoApprovers + DNS grant, nothing else). The ordinary
   full-merge `plan` path is untouched and remains byte-identical when the new
   subcommand is not used.
3. **State file** → own `generated/connector-switch-state.json`
   (`schema_version: 1`); `failover-state.json` untouched; multi-fallback's
   schema bump decouples.
4. **Interaction with native oldest-first** → composition: native HA operates
   within each pool; forced selection operates between pools; no automated
   switching in v1.3.
5. **Readback-mismatch posture** → no automatic second write; report + recovery
   commands + non-zero exit (fail-closed against concurrent edits).
