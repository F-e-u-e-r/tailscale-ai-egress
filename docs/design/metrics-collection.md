# Design: metrics collection (counters + liveness)

**Status:** implemented (counters + liveness + latency_ms + Prometheus textfile).
Controller reuse is the remaining named follow-up.
**Tracking:** [Roadmap](../Roadmap.md) · governed by [Stability](../Stability.md)

## Do we need to wrap an app for Tailscale? No.

The goal is to surface upload/download usage, latency, and uptime for the failover
controller and the connector monitor. Tailscale's own CLI/daemon already expose
the **raw local inputs** for all three — no GUI, no server:

- **Usage (raw counters):** `tailscale status --json` per-peer `TxBytes` /
  `RxBytes` (cumulative since tailscaled started; reset on restart);
  `tailscale metrics print` emits node-level Prometheus
  `tailscaled_{inbound,outbound}_bytes_total{path=...}`.
- **Latency (spot sample):** `tailscale ping <peer>` (the health engine already
  pings).
- **Liveness (point-in-time):** `status --json` `Online`, `Active`,
  `LastHandshake`, `LastSeen`, `Created`, `Relay`, `CurAddr`.

Three distinct scope cases, none of which we build:

1. A **GUI app** — out of scope ([Stability](../Stability.md): no GUI).
2. A **project-operated** agent / hosted dashboard / telemetry / server — out of
   scope (no telemetry, no project server).
3. A **user-operated** scraper (Prometheus / node_exporter / Grafana / Uptime
   Kuma) — an allowed *consumer* the operator runs, not something we wrap.

**Honest limits:** true monthly/billing usage and historical uptime-% are
*derived* metrics that need external persistence (the operator's scraper or the
VPS provider's meter). The bytes we expose are **cumulative counters since
tailscaled started**, good for `rate()`, not a "total this month". We never label
them "monthly" or "session" usage.

## The metrics object (single normative shape)

`scripts/health_check.py peer_metrics()` returns ONE object with a FIXED key set;
keys are never omitted. Two null regimes:

- **Transport/resolution failure** (status unavailable, or the peer label does not
  resolve): a **null-filled metrics object** — all keys present, every value
  `null`.
- **Peer resolved:** populate what exists; individual missing / zero-value fields
  are `null`, but keys are still never omitted.

| Field | Source | Type / semantics |
|---|---|---|
| `tx_bytes_total`, `rx_bytes_total` | status TxBytes/RxBytes | int\|null. Cumulative since tailscaled start. |
| `online`, `active` | status Online/Active | bool\|null |
| `last_handshake` | status LastHandshake | RFC3339 string\|null (null on the Go zero-value `0001-01-01…`) |
| `last_handshake_age_seconds` | derived, UTC | int\|null (≥ 0) |
| `relay`, `cur_addr` | status Relay/CurAddr | string\|null (raw, for transparency) |
| `connection_path` | derived | `direct` \| `derp` \| `unknown` \| null |
| `latency_ms` | `tailscale ping` RTT | float\|null. Stamped ONLY on a resolved peer; null when unmeasured or the ping had no RTT. |

`active` is surfaced as raw liveness only; it does NOT participate in
`connection_path`.

### `connection_path` derivation — observed, not proven

Verified on this host with **Tailscale 1.98.2**: `Relay` was set for *every*
observed peer (it is the home / preferred DERP region), so `Relay` must NOT be the
discriminator. `CurAddr` (the current direct address) matched the `tailscale
status` "direct/relay" text on the two peers with a determinate path (2/2).

- `direct` if `CurAddr` is non-empty;
- `derp` if `CurAddr` is empty AND `online` is exactly boolean `true`;
- `unknown` otherwise (empty `CurAddr` with `online` false/null/absent);
- `null` only inside the null-filled failure object.

`online` counts as online only when the JSON boolean is exactly `true`. This is a
**best-effort** derivation; consumers needing certainty should use the raw
`cur_addr` / `relay` fields or the `tailscale status` text. Re-check the rule on
Tailscale version upgrades. The health engine ships raw `relay`/`cur_addr` so the
derived enum can be refined additively later without breaking the fixed key set.

## Where it is surfaced

- **`peer-metrics --node <label>` subcommand** (`health_check.py`): prints the
  object as JSON. **Always exits 0** when it can print the object (including the
  null-filled object); non-zero only for usage/arg errors. This is the single
  source of truth for the shape.
- **`connectors` subcommand** (which `monitor-connectors.sh` delegates to): each
  connector record in `--json` gains a `metrics` object (additive; the report's
  `schema_version` is unchanged). Text mode appends one `[metrics] connector=…
  tx=… rx=… path=… handshake_age=… latency_ms=…` line per connector (nulls render
  as `-`); existing tokens are append-only (never reworded/reordered) and message
  ids are unchanged.

> Note vs. the reviewed plan §4: because `monitor-connectors.sh` delegates its
> whole report to `health_check.py connectors`, there is no bash-side merge point.
> The metrics are attached inside `cmd_connectors` using the status it already
> fetched (one fetch, not a per-connector shell-out), which is cleaner and keeps
> `health_check.py` as the single owner of the shape. `peer-metrics` exists for
> standalone / future controller use.

**Non-gating (hard rule):** a metrics fetch/extract failure never changes the
monitor's exit code, existing health/status lines, or message ids. The
`overall_healthy` verdict is driven ONLY by the pre-existing
reachability/online/route checks and does not reference `metrics`.

## Additive-safety & consumer rules

- JSON: only the new `metrics` key (and its fixed sub-keys) is added; existing
  keys, meanings, types, flags, and message ids are unchanged. `schema_version`
  stays as-is.
- Consumers: ignore unknown keys; treat `metrics` as optional (absent on
  pre-metrics builds ⇒ unavailable, never a failure). The `peer-metrics` object
  itself never omits sub-keys (null-filled on failure).

## Peer resolution & nullability

- A configured label (hostname / MagicDNS / Tailscale IP / node ID) resolves to a
  `status --json` peer via the health engine's existing `resolve_identity`
  precedence; not found ⇒ null-filled object.
- Offline peers may carry stale Tx/Rx and an old `LastHandshake`;
  `last_handshake_age_seconds` makes staleness visible.
- `status --json` does not require root; the extractor is read-only.
- `latency_ms` is a *per-peer* metric measured by `tailscale ping`, stamped ONLY
  once the peer resolves. The `connectors` report reuses its single reachability
  ping (no extra ping); `peer-metrics --ping` (opt-in, default off) resolves first
  and pings only a resolved peer, so an unresolvable label is never pinged and the
  object stays null-filled. A caller-supplied RTT is validated (finite, ≥ 0).

## Prometheus textfile (`monitor-connectors.sh --prometheus-textfile <path>`)

For a user-run scraper (node_exporter's textfile collector, Prometheus, Grafana),
the monitor can emit the per-connector state as a `.prom` textfile instead of the
normal report. **Python owns the whole thing** — `health_check.py connectors
--prometheus [--output <path>]` builds a validated Prometheus document and, with
`--output`, writes it **atomically** (same-dir `mkstemp` → `fchmod 0644` → `fsync`
→ `os.replace`); the shell wrapper only passes the path and, in `--watch`, keeps
looping (a failed write leaves the previous file untouched). It is mutually
exclusive with `--json`.

Gauges (labels `connector` = primary|fallback, `label` = hostname):
`ai_egress_connector_online`, `_reachable`, `_latency_ms` (the probe RTT, sourced
from the reachability ping so it survives even an unresolved peer),
`_tx_bytes_total` / `_rx_bytes_total` (counters), `_last_handshake_age_seconds`,
`_routes`, `_info{connection_path=…}`, and `ai_egress_overall_healthy` (emitted
**last**, the write-completeness sentinel).

**Never a silently-wrong value (rule 4):** a null, non-finite, negative-counter,
float64-unrepresentable, or malformed-route value is **omitted** (no sample line),
never a fake `0`; route counts are re-validated per element with `ipaddress`. This
strict per-element route validation is intentionally stricter than the released,
lenient `node_routes` check that drives `overall_healthy`: for any well-formed
status they agree, and only an adversarial/malformed route field (e.g. `0.0.0.1/0`
or `not-a-cidr`, which real Tailscale never emits) can make the strict `_routes`
gauge and the health sentinel diverge. Tightening the health verdict itself is a
released-semantics change kept OUT of this additive step (tracked as a separate
hardening PR); metrics never change the health verdict here (rule 1). **Exit code = write
integrity, not health:** with `--output` a successful write exits `0` even when the
pair is degraded (health is the `ai_egress_overall_healthy` gauge); a
generation/write failure exits non-zero. Node-level `tailscaled_*` counters are
already Prometheus-formatted and separately scrapeable via `tailscale metrics
print`, so they are not wrapped here (a future opt-in could add them, validated).

**Operator note (security):** point `--prometheus-textfile` at a directory writable
only by the writer (as node_exporter's textfile collector expects). The atomic
`os.replace` cannot defend against a pathname-swap in a **world-writable,
non-sticky** parent, so such a directory offers no integrity guarantee. The writer
also refuses to publish a document that is not sentinel-terminated (a truncated or
partial generation fails rather than clobbering a good file).

## Follow-ups (not in this step)

- **Controller reuse:** the failover controller may consume `peer-metrics` for
  post-switch diagnosis/logging — best-effort / non-gating; metrics must never
  change a failover decision or the controller's exit path in 1.x.
