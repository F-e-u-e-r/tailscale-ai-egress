# Multi-Machine Failover

This guide focuses on AI egress. The examples below keep AI-related domains on a predictable Tailscale App Connector path while normal traffic stays local or on the user's chosen exit node.

Use this guide when you want two connector machines in the same egress region, usually across different providers, so a second machine can take over new matching connections if the first machine goes offline. For the single-machine VPS flow, start with [AWS Lightsail Notes](AWS-Lightsail.md), [WebARENA Notes](WebARENA.md), or [Generic VPS Notes](Generic-VPS.md).

Official Tailscale references:

- [App connectors](https://tailscale.com/kb/1281/app-connectors)
- [High availability](https://tailscale.com/kb/1115/high-availability)

## Two Deployment Models

This project supports two connector deployment models. Pick yours before
reading on:

- **Model A — one tag, native HA (default, recommended).** All connectors in
  the pair share one connector tag and Tailscale handles failover natively.
  Everything from [Intended Topology](#intended-topology) through
  [Health Checks](#health-checks) below — topology, oldest-first primary
  selection, bootstrap, the second-machine policy prompt, verification —
  describes Model A, and the later
  [Exit-Node Failover](#exit-node-failover-full-traffic) and
  [Taking One Machine Out](#taking-one-machine-out) sections apply to both
  models. If you do not have a reason to force which pool egresses your AI
  traffic, use Model A and skip the Advanced Mode section.
- **Model B — distinct-tag pools with forced selection (advanced, opt-in,
  planned for v1.3).** Primary and fallback connector pools live under
  distinct tags, and an operator-invoked, auditable switch forces which pool
  serves the AI domain set. It composes with — and does not replace — native
  HA. See
  [Advanced Mode: Distinct-Tag Active Connector Switch (Model B)](#advanced-mode-distinct-tag-active-connector-switch-model-b).

## Intended Topology

The intended pool is same-region, multi-provider failover:

| Provider | Hostname | Connector Tag |
| --- | --- | --- |
| AWS Lightsail | `ai-egress-jp-aws-01` | `tag:ai-egress-jp` |
| WebARENA | `ai-egress-jp-web-01` | `tag:ai-egress-jp` |

Both machines use the same connector tag because they serve the same egress region and the same AI domain set. If you intentionally use cross-region egress, use different tags such as `tag:ai-egress-jp` and `tag:ai-egress-us` to avoid unexpected latency or region switching. That cross-region advice is about separate per-region deployments — each region's pool still uses one tag, so it is Model A per region. Forcing which of two distinct-tag pools serves a *single* AI domain set is a different, advanced workflow: see [Advanced Mode (Model B)](#advanced-mode-distinct-tag-active-connector-switch-model-b).

AWS Lightsail and WebARENA may differ in IPv6 support, firewall defaults, and bandwidth quota. Bring each provider up as a working single connector before pairing them for failover. This is connector failover, not exit-node failover; normal traffic should stay local unless a client deliberately selects an exit node.

## Choosing The Primary (Oldest-First)

Tailscale's default connector failover is **oldest-first**: connector selection follows the order in which connectors joined the tailnet, the oldest connector is the primary, and failover proceeds oldest-first. This is the default behavior on all plans (see [High availability](https://tailscale.com/kb/1115/high-availability)). The per-client pseudorandom, "sticky" selection described for *regional routing* is a separate Premium/Enterprise feature and does not apply to this default.

So you do not need policy edits to get a deterministic primary/fallback pair — just control the join order. To make WebARENA the primary and AWS the fallback:

1. If needed, remove or re-authenticate both connector machines so their join order is clean.
2. Bring up **WebARENA first** and let it come online.
3. Bring up **AWS second**.
4. Keep both online with the same connector tag and the same AI domain set.

Tailscale then treats WebARENA (the oldest) as primary and fails over to AWS when WebARENA goes offline. With `tailscale down`, failover takes up to ~15 seconds; a network partition can take longer. Within one tag, this project intentionally does not reassign connectors via the policy API — native oldest-first ordering is enough for a primary/fallback pair. For forced pool-level selection *across distinct tags*, see [Advanced Mode (Model B)](#advanced-mode-distinct-tag-active-connector-switch-model-b).

## Bootstrap The Two Machines

On the AWS Lightsail VPS:

```bash
REGION=JP CONNECTOR_HOSTNAME=ai-egress-jp-aws-01 ./bootstrap.sh
```

On the WebARENA VPS:

```bash
REGION=JP CONNECTOR_HOSTNAME=ai-egress-jp-web-01 ./bootstrap.sh
```

Use a Tailscale node auth key (`tskey-auth-...`) tagged or allowed for `tag:ai-egress-jp` on both machines. If `tag:ai-egress-jp` is not available in the auth key dialog, apply the generated tailnet policy first, then generate the key again. Do not paste a `tskey-api-...` token at the node auth prompt; API tokens are only for Admin Console policy automation.

## Second-Machine Policy Prompt

If the first machine already applied a policy containing `tagOwners`, `autoApprovers`, and `nodeAttrs` for `tag:ai-egress-jp`, answer `n` when the second machine asks whether the installer should update your Tailscale policy automatically. The policy already contains the shared connector tag, so this avoids extra API calls and plan bundles.

If you are unsure, answering `y` is safe **in Model A** — `scripts/policy_tool.py plan` merges policy idempotently, so it should preserve existing entries and create a reviewable plan before any apply. In Model B this advice does **not** transfer: when provisioning a fallback-tag node, answer `n` — see [Adopting Model B](#adopting-model-b-a-to-b).

Optional visibility check before the second bootstrap:

```bash
tailscale status | grep -E 'tag:ai-egress-jp|ai-egress-jp-' || \
  echo "No ai-egress-jp connector visible from this client yet"
```

If you prefer a UI check instead, use the Tailscale Admin Console Machines page for device/tag visibility and the policy page for connector policy details.

## Verification

Verify that both machines are visible:

```bash
tailscale status | grep ai-egress-jp
```

Verify client routes for configured AI domains:

```bash
./check-client-routes.sh
```

The checker tests every IPv4 A record and reports `[OK]`, `[WARN]`, or `[FAIL]` (it also inspects IPv6/AAAA advisorily when present; IPv6 findings never fail). A short `[WARN]` period can be normal while App Connector DNS discovery and route advertisement settle.

To test failover, stop `tailscaled` on one connector, wait for Tailscale clients to detect the route change, and recheck the route or browser behavior from a client. Existing connections may break when a connector disappears. New connections should move to another online connector after Tailscale detects the route change.

## Health Checks

At minimum, monitor that each connector is online in Tailscale and that sample AI domains still resolve to a Tailscale route from a client:

```bash
tailscale status | grep ai-egress-jp
./diagnose.sh
```

For lightweight server-side monitoring, run diagnostics from cron and send the output to your normal alerting or log collection path:

```cron
*/5 * * * * cd /path/to/tailscale-ai-egress && ./diagnose.sh >/var/log/tailscale-ai-egress-diagnose.log 2>&1
```

For production use, pair this with provider-level monitoring for VPS reachability, bandwidth quota, and disk space. Tailscale failover helps with connector availability, but it does not replace VPS health or billing/quota alerts.

### Connector Monitor (`monitor-connectors.sh`)

`monitor-connectors.sh` is a read-only helper that checks both connectors in a primary/fallback pair. It reports each connector's online state and tailnet reachability (via `tailscale ping`), and — only when a `TAILSCALE_API_KEY` is configured — the `device.created` ordering, so you can confirm the intended primary is the oldest. Without an API token it prints `ordering=unavailable` and still runs the health checks; ordering uses an API key (`TAILSCALE_API_KEY`) only (OAuth is not used for this optional check). It also reports `routes_serving` — which connector is advertising app-connector routes — and degrades when neither is; set `REQUIRE_ROUTES=0` if your client cannot observe connector routes. This route check is best-effort: it confirms a non-default route is advertised, not that specific AI app-domain routes resolve. It never switches anything. Both the monitor and the controller require the local Tailscale backend to be `Running`; if it is `Stopped`, `NeedsLogin`, or `Starting`, the monitor reports degraded and the controller makes no switch (fails closed). The monitor never switches anything in either deployment model — including [Model B](#advanced-mode-distinct-tag-active-connector-switch-model-b), where switching is a separate, operator-invoked tool.

```bash
PRIMARY_CONNECTOR=ai-egress-jp-web-01 FALLBACK_CONNECTOR=ai-egress-jp-aws-01 \
  ./monitor-connectors.sh --once
```

Exit status is `0` when both connectors are online and reachable and `1` when degraded, so it suits cron alerting:

```cron
*/5 * * * * cd /path/to/tailscale-ai-egress && ./monitor-connectors.sh --once >>/var/log/tailscale-ai-egress-monitor.log 2>&1
```

## Advanced Mode: Distinct-Tag Active Connector Switch (Model B)

> **Status: planned for v1.3 — not yet released.** The design is merged
> ([design/connector-failover-apply.md](design/connector-failover-apply.md)),
> and these docs are the operator contract the implementation must satisfy
> (they merge first, by the design's pre-implementation gate). The commands in
> this section are **not available in any released version yet**; every
> command block below is marked accordingly. The implementation PR that ships
> them removes this banner and the per-block markers.

### The two models, precisely

Model A (everything above this section) keeps all connectors of a pair under
**one** tag; Tailscale's native HA picks and fails over within that pool, and
nothing in this project mutates connector selection. If that serves you, you
do not need Model B.

Model B puts the primary and fallback pools under **distinct** tags (for
example `tag:ai-egress-jp` and `tag:ai-egress-jp2`). The app-connector
policy's `connectors` list names the **active** pool; an operator-invoked
switch rewrites that one list through the audited plan-bundle pipeline.
**Model B does not replace Tailscale-native HA; the two compose:** native HA
keeps operating *inside* each pool exactly as in Model A, and the switch moves
the AI domain set *between* pools. Each pool may still hold several nodes.

### When to use it

| Situation | Native HA reacts? | Model B switch helps? |
| --- | --- | --- |
| Node offline / unreachable / route withdrawn | **yes** | not needed |
| Egress IP wrong (geo/reputation/provider NAT change) | no — node looks healthy | **yes** |
| Provider path degraded (loss/latency, peering incident) | no | **yes** |
| Provider quota / rate-limit exhausted for the AI service | no | **yes** |
| Planned maintenance / deliberate evacuation | no | **yes** |
| Automatic, unattended failover on health signals | native HA already covers the node-down case | **no — out of scope in v1.3** |

Model B exists for the **online-but-bad** primary: conditions Tailscale's
native oldest-first selection cannot see because the node itself looks
healthy. There is deliberately no automatic watcher in v1.3 — the switch is a
one-shot, operator-invoked action.

### How it works

The operator entry point is `failover-connectors.sh`, with three forms:

```bash
# v1.3 — not yet released
./failover-connectors.sh
```

Report mode: shows the managed app-connector entry and its current
`connectors` value from the live policy, declaration checks for both pool
tags, both pools' node liveness (from `tailscale status --json` peer tags),
and state-file drift. It mutates nothing and writes nothing. Without a policy
credential the policy-derived fields (active pool, declarations, drift) show
`unavailable`; the status-derived liveness fields still work.

```bash
# v1.3 — not yet released
./failover-connectors.sh --to tag:<pool>
```

Plan only: runs the fail-closed precondition checks, generates an auditable
plan bundle via the connector-scoped planner (`policy_tool.py connector-plan
--switch-to`), prints its diff, and **stops** — the tailnet is not written;
the bundle sits under `generated/policy-plans/` for review.

```bash
# v1.3 — not yet released
./failover-connectors.sh --to tag:<pool> --apply
```

Additionally re-checks the volatile preconditions (the target pool still has
an online tagged node) immediately before `policy_tool.py apply-plan`, which
keeps its existing exact `APPLY <plan-id>` confirmation and If-Match/ETag
conflict detection. After applying, it **reads back** the policy and verifies
the `connectors` list equals the target. Switching back is the same command
pointed at the other tag.

The pool pair is **declared configuration**: `PRIMARY_CONNECTOR_TAG` and
`FALLBACK_CONNECTOR_TAG` in `failover.env` name the two pools (see
[Configuration](Configuration.md)). Which pool is *active* is always read
from the live policy, never assumed.

#### Refusals, warnings, and recovery

The switch is fail-closed: anything unclear refuses with a specific pointer.
What you will actually see, and what to do:

| What you see | What it means | What to do |
| --- | --- | --- |
| Refusal: pool-pair keys missing, invalid, or not distinct | Model B is not configured | Set both `*_CONNECTOR_TAG` keys ([Configuration](Configuration.md)) |
| Refusal: `--to` names a tag outside the configured pair | The supported path never switches to an undeclared third tag | Fix the keys, or reconsider the target |
| Drift refusal: live `connectors` is empty, multi-tag, or outside the pair (verbatim value printed) | The policy was changed out of band; the scripted path will not guess | Review, then reconcile: a reviewed raw `connector-plan --switch-to tag:<pool>` (its one-element bundle diff IS the reconciliation), or an Admin Console edit |
| Refusal: "already active" | The current value is exactly the target | Nothing to do — no bundle is generated; state file and cooldown clock untouched |
| Report shows policy fields `unavailable` | No policy credential | Report degrades to status-only; a switch (`--to` / `--apply`) refuses instead |
| Refusal: managed entry missing, or duplicate `--connector-name` matches | Ambiguous target entry | Fix `--connector-name` (default `AI-Egress-<REGION>`) or the policy |
| Refusal: target tag not in `tagOwners`, or autoApprovers/DNS-grant readiness missing | The target pool is not declared | Run the [`--declare` setup step](#adopting-model-b-a-to-b) |
| Refusal: target pool has zero online tagged nodes (at plan time, and re-checked right before apply) | Nowhere safe to go | Bring an online node up in the **target** pool first (whichever pool `--to` names) |
| Refusal: status or policy unreadable or ambiguous | Fail-closed on bad input | Investigate `tailscale status --json` / the credential; retry when clean |
| Refusal while planning: the policy changed between the script's pre-flight and the planner's fetch | Concurrent-edit protection (the scripted path pins the expected `connectors` value; the planner re-checks every policy-derived precondition against its own fetched snapshot) | Re-run the switch |
| `apply-plan` refuses with an ETag conflict (412) | The policy changed after the bundle was created | Regenerate the plan; nothing was written |
| Cooldown warning (last switch younger than `CONNECTOR_SWITCH_COOLDOWN`; shows the previous switch's timestamp and plan id) | Anti-fat-finger advisory, not a lockout | The `APPLY <plan-id>` confirmation still stands — proceed only deliberately |
| Readback mismatch after apply (re-read once for propagation lag) | The policy did not read back as the target | **Nothing further is written automatically**; the tool reports loudly and exits non-zero — see [Rollback](#rollback-model-b) |

**Dual-tagged nodes — a prominent warning, not a refusal:** report and switch
output name any node carrying BOTH pool tags. Such a node stays active in
either pool, so a switch cannot evacuate it. The switch is not refused (hard
refusal would deadlock legitimate transitional retagging), but Model B assumes
pool membership is disjoint — resolve dual-tagged nodes promptly.

**Notify hook:** on a completed switch and on a failed readback, the existing
`FAILOVER_NOTIFY_CMD` hook fires with
`FAILOVER_EVENT=connector-switch|connector-switch-readback-failed`,
`FAILOVER_ROLE`/`FAILOVER_LABEL` carrying the pool tags, `FAILOVER_REASON`,
plus `FAILOVER_PLAN_ID`. No hook configured = no-op; hook failure never
changes the switch outcome (same contract as the exit-node controller's
hook).

**Expert escape hatch — know exactly what it skips:** driving
`policy_tool.py connector-plan --switch-to` + `apply-plan` directly bypasses
the ENTIRE script layer: the pool-pair configuration checks, the scripted
drift refusal, BOTH target-pool liveness checks (the plan-time precondition
and the apply-time recheck — liveness comes from `tailscale status`, which the
planner never reads), and the cooldown warning. A raw switch can therefore
target a pool with zero online nodes. It does NOT bypass the planner's own
snapshot checks: the managed entry must match `--connector-name` exactly
once, the target must be declared and ready, `--switch-to` takes exactly one
tag, the bundle diff may touch nothing but that one list, and a current value
already equal to `[target]` is refused. The supported operator path is the
script; the raw subcommand is for experts and for drift reconciliation.

### Adopting Model B (A to B)

Order matters. Steps 1 and 3–4 change no routing; step 2's retag path is the
one exception, flagged below.

1. **Declare the fallback pool in policy first:**

   ```bash
   # connector-plan: v1.3 — not yet released (apply-plan already ships today)
   python3 scripts/policy_tool.py connector-plan --declare tag:<fallback>
   python3 scripts/policy_tool.py apply-plan generated/policy-plans/plan.<plan-id>
   ```

   Review the bundle diff before applying: it must show ONLY the three
   declaration surfaces — `tagOwners` for the fallback tag, both
   `autoApprovers.routes` entries (`0.0.0.0/0`, `::/0`), and the member→pool
   DNS grant — and must NOT touch `connectors`. That absence is what makes
   declaring a routing no-op. After this step, node registration under the
   new tag becomes possible (Tailscale rejects `--advertise-tags` for an
   undeclared tag).
2. **Provision the fallback pool.** Preferred: a **new node** — follow
   [Bootstrap The Two Machines](#bootstrap-the-two-machines) with these
   Model B overrides: set the fallback tag EXPLICITLY
   (`CONNECTOR_TAG=tag:<fallback> CONNECTOR_HOSTNAME=... REGION=...
   ./bootstrap.sh` with the fallback tag's auth key — `CONNECTOR_TAG` must be
   set, because `REGION` alone derives the original region tag and would
   advertise the Model A tag instead; the bootstrap already advertises BOTH
   `--advertise-connector` and `--advertise-tags`, which an app connector
   needs), **answer `n` at the policy prompt, and do not manually merge the
   generated policy snippet** — the ordinary policy path is a full additive
   merge that would union the fallback tag into `connectors` and create the
   dual-active state ([warning 3](#warnings-model-b)); step 1's `--declare`
   already established everything the node needs. Alternative: **retag an
   existing node — a maintenance-window operation, NOT a routing no-op**:
   moving a node out of the active pool changes that pool's membership
   immediately (native selection fails over inside the pool; brief disruption
   is possible), and if it was the pool's only online node this is an outage
   until the move completes. **Verify the primary pool keeps ≥ 1 other online
   node first.** Prefer the Admin Console for the retag — the CLI path
   restates every non-default flag (see
   [Troubleshooting](Troubleshooting.md#tailscale-up-complains-about-non-default-flags)),
   and auth-key-tagged nodes carry additional retagging restrictions.
3. **Set the pool pair** in `failover.env`: `PRIMARY_CONNECTOR_TAG` /
   `FALLBACK_CONNECTOR_TAG` (see [Configuration](Configuration.md)).
4. **Verify:** run the report (`./failover-connectors.sh`, no args) — both
   pools declared and green, preconditions met. With the new-node path,
   traffic behavior until a switch is applied is byte-for-byte what Model A
   produced.
5. **Ongoing:** after any switch, routine connectors-touching plans must
   follow the source-of-truth rule — see [warning 3](#warnings-model-b) (the
   post-switch merge hazard).

### Returning to Model A (B to A)

1. If the fallback is currently active, switch back to the primary pool
   first (`./failover-connectors.sh --to tag:<primary> --apply`).
2. Retire or retag the fallback nodes.
3. Remove `PRIMARY_CONNECTOR_TAG` / `FALLBACK_CONNECTOR_TAG` from
   `failover.env`; optionally delete `generated/connector-switch-state.json`.
4. Optional declaration cleanup: removing the fallback tag's `tagOwners` /
   `autoApprovers` / DNS-grant entries is **not expressible in the 1.x
   add-only pipeline** — it is a manual Admin Console edit. Leaving the
   declarations in place is explicitly safe: a declared tag with no nodes and
   no `connectors` reference routes nothing.

### Rollback (Model B)

Three layers, preferred first:

1. **Compensating switch-back** (connector-only by construction, so it is
   safe under concurrent policy edits):

   ```bash
   # v1.3 — not yet released
   ./failover-connectors.sh --to tag:<previous> --apply
   ```

   Or the two-step variant: plan first (`--to tag:<previous>`), review the
   bundle diff, then re-run with `--apply`. If the live `connectors` value has
   drifted (empty/multi-tag/out-of-pair), the scripted path refuses — use the
   reviewed raw flow instead: `connector-plan --switch-to tag:<pool>` then
   `apply-plan` (see the escape-hatch notes above). The state file remembers
   `previous_connectors`, and report mode shows the live value.
2. **`policy_tool.py restore-plan <bundle>`** — restores the exact pre-switch
   policy captured in the bundle (existing, tested machinery; requires
   `RESTORE <plan-id>`). **Two caveats:** it rewrites the **whole** policy
   from the captured snapshot, so any policy edit made after the switch —
   related to the switch or not — is lost; use it only when the policy has
   not otherwise changed since the switch. And a bare `restore-plan`
   intentionally does not touch `generated/connector-switch-state.json`, so
   report mode will show policy-vs-state drift (advisory) until the next
   successful scripted switch.
3. **Manual fallback:** edit the `connectors` list in the Admin Console — the
   diff is one array.

### Warnings (Model B)

1. The switch **overrides** which pool Tailscale would natively serve; you
   own the consequences of pinning traffic to the fallback pool (different
   egress IPs/geo/reputation, provider cost, latency).
2. Model B requires disciplined tag hygiene: both tags must stay owned,
   auto-approved, and DNS-granted, or a switch will strand routes or DNS.
   The preconditions catch this, but only at switch time — set-up drift is on
   the operator.
3. **Post-switch merge hazard:** the ordinary merge path is add-only by
   design; a routine re-plan that still lists the old primary pool re-unions
   it into `connectors` (dual-active) and silently undoes the switch. After a
   switch, connectors-touching plans must name only the intended active pool
   or use the connector-scoped mode — that is, `connector-plan` bundles,
   which by construction touch only the connector slice and cannot re-union
   the old pool. (This is routine post-switch plan hygiene that keeps the
   *current* selection; it is distinct from the expert escape hatch above,
   which *changes* the selection outside the script's checks.)
4. Concurrent hand-edits of the policy race the switch; `apply-plan`'s
   If-Match detects and refuses (regenerate the plan). For rollback under
   concurrency, prefer the compensating switch-back; `restore-plan` rewrites
   the whole policy (see [Rollback](#rollback-model-b)).
5. Policy readback confirms the *policy* changed, not that every client's
   routes have converged; allow for route propagation (typically seconds to a
   couple of minutes) before judging the switch by client behavior.
6. In connector-only mode with no exit node, a switch mid-flow breaks
   existing connections; new connections move after route propagation (the
   same caveat as native failover, restated here deliberately).
7. The state file records what *this tool* did; out-of-band changes (console
   edits, a bare `restore-plan`) are detected at report time by comparing
   policy reality, not assumed.

## Exit-Node Failover (Full Traffic)

Connector failover above only covers the selected AI domains. If you also route *all* other traffic through an exit node, note that Tailscale does not provide priority-ordered exit-node failover (only `--exit-node=auto:any`, which selects by latency rather than a fixed primary/fallback order). `failover-exit-node.sh` adds a client-side primary/fallback exit-node controller for macOS and Linux.

It probes a primary and fallback exit node and, when run with `--apply`, switches the local `tailscale set --exit-node` to the fallback when the primary fails its tailnet ping, then switches back when the primary recovers (unless `RESTORE_PRIMARY=0`). It is observe-first: without `--apply` it only reports the proposed action, and it only ever switches to a node that passed its ping in the same cycle.

By default, the controller manages an *already selected* exit node and does not
turn one on by itself. Select your primary once first, or explicitly let the
controller make the initial selection with `--ensure-primary`:

```bash
cp examples/failover.env.example generated/failover.env   # then edit PRIMARY/FALLBACK exit nodes

# Option A: select the primary yourself once, then run the controller.
tailscale set --exit-node=<your-primary>
./failover-exit-node.sh --watch --apply

# Option B: let the controller select the primary when none is set.
./failover-exit-node.sh --watch --apply --ensure-primary

# Observe first (no change) at any time:
./failover-exit-node.sh --once
```

iOS and Android cannot run this watcher (switch the exit node in the app); Windows is not yet supported. See [Configuration](Configuration.md) for the full list of `failover.env` settings, and the `docs/examples/` directory for ready-made systemd, OpenRC, launchd, and cron units.

### Post-Switch Diagnostics With `peer-metrics`

The controller ships **no built-in** post-switch metrics collection: under the 1.x
rule its *own code* never puts a metrics fetch on the failover decision or exit path,
so by default a slow or failing metric cannot stall or fail a switch.

You can still log the *new* exit node's read-only metrics after each switch by
composing the existing `FAILOVER_NOTIFY_CMD` hook with the `peer-metrics` subcommand.
This needs **no controller code change** and **cannot change the failover decision or
the already-recorded switch/cooldown** — the switch is recorded before the hook runs,
and the hook's exit status is ignored. It does, however, **extend the controller's
process path while it runs**: the hook is *synchronous*, so keep it fast (a slow
command stalls the watch loop), and a `SIGTERM` reaching the controller *while the
hook is running* makes it exit `143` rather than `0`. That is your informed opt-in,
exactly as for any `FAILOVER_NOTIFY_CMD`. The hook receives `FAILOVER_EVENT`
(`switched`|`failed`), `FAILOVER_ROLE`, `FAILOVER_LABEL`, and `FAILOVER_REASON` in its
environment; gate on `switched` so it does not run on a failed attempt:

```bash
# Log the new exit node's tx/rx, connection path, latency, and handshake age after
# each successful switch (adjust the install path). `peer-metrics --ping` is
# read-only and always exits 0; see docs/design/metrics-collection.md.
export FAILOVER_NOTIFY_CMD='[ "$FAILOVER_EVENT" = switched ] && \
  python3 /opt/tailscale-ai-egress/scripts/health_check.py \
    peer-metrics --node "$FAILOVER_LABEL" --ping | logger -t ai-egress-failover'
./failover-exit-node.sh --watch --apply
```

## Taking One Machine Out

| Action | Effect |
| --- | --- |
| `sudo systemctl stop tailscaled` | Temporarily takes a systemd-based connector offline; it may return after service restart or reboot |
| `sudo rc-service tailscale stop` | Alpine/OpenRC alternative for temporarily taking the connector offline |
| `sudo tailscale logout` | Disconnects the node from the tailnet; the machine record may remain in the Admin Console |
| Remove machine in Admin Console | Removes the device record from the tailnet |

In Model B, if the machine you are taking out is the active pool's last online
node, [switch the active pool](#advanced-mode-distinct-tag-active-connector-switch-model-b)
first — otherwise the AI domain set is left with no online connector.
