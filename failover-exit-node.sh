#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="$(cat "$ROOT_DIR/VERSION" 2>/dev/null || true)"
VERSION="${VERSION:-1.3.0}"

# Initialised early so the cleanup trap is safe even on Bash 3.2 early exits.
tmp_files=()
LOCK_HELD=0
LOCK_DIR="${LOCK_DIR:-}"
LOCK_WAIT="${LOCK_WAIT:-}"

HEALTH="$ROOT_DIR/scripts/health_check.py"

# Config precedence: explicit environment > generated/failover.env > built-in default.
GENERATED_DIR="${GENERATED_DIR:-$ROOT_DIR/generated}"
FAILOVER_ENV="${FAILOVER_ENV:-$GENERATED_DIR/failover.env}"
PRIMARY_EXIT_NODE="${PRIMARY_EXIT_NODE:-}"
FALLBACK_EXIT_NODE="${FALLBACK_EXIT_NODE:-}"
PROBE_TARGET="${PROBE_TARGET:-}"
CHECK_INTERVAL="${CHECK_INTERVAL:-}"
FAIL_THRESHOLD="${FAIL_THRESHOLD:-}"
OK_THRESHOLD="${OK_THRESHOLD:-}"
COOLDOWN="${COOLDOWN:-}"
PING_TIMEOUT="${PING_TIMEOUT:-}"
PROBE_HTTP_TIMEOUT="${PROBE_HTTP_TIMEOUT:-}"
RESTORE_PRIMARY="${RESTORE_PRIMARY:-}"
ENSURE_PRIMARY="${ENSURE_PRIMARY:-}"
STATE_FILE="${STATE_FILE:-}"
READBACK_DELAY="${READBACK_DELAY:-}"
# Opt-in notification hook, environment only (NOT parsed from failover.env: a
# command string does not survive the env-file parser's quoting rules).
FAILOVER_NOTIFY_CMD="${FAILOVER_NOTIFY_CMD:-}"

APPLY=0
WATCH=0
EGRESS=0
DRY_RUN="${DRY_RUN:-0}"
USE_SUDO="${AI_EGRESS_USE_SUDO:-1}"

usage() {
  cat <<'EOF'
Usage: ./failover-exit-node.sh [--once|--watch] [--apply] [--dry-run] [--egress]

Client-side exit-node failover controller (macOS + Linux). Probes a primary and
fallback exit node via scripts/health_check.py and, when told to, switches the
local `tailscale set --exit-node` between them.

Safety: observe-first. Without --apply this only reports the proposed action.
iOS/Android cannot run this watcher (switch the exit node in the app); Windows
is not yet supported.

Options:
  --once       Run a single evaluation cycle (default).
  --watch      Loop forever, evaluating every CHECK_INTERVAL seconds.
  --apply      Actually run `tailscale set --exit-node` (otherwise observe only).
  --dry-run    With --apply, print the privileged command without running it.
  --egress     Also HTTP-probe the egress of the currently active exit node.
  --ensure-primary  When no exit node is selected, select the primary once it is
                    reachable (opt-in; otherwise the controller imposes nothing).
  --version    Print the tailscale-ai-egress version and exit.
  -h, --help   Show this help.

Configuration (environment or generated/failover.env):
  PRIMARY_EXIT_NODE, FALLBACK_EXIT_NODE   exit node hostname / MagicDNS / IP
  PROBE_TARGET (https://ipinfo.io)        egress probe URL
  CHECK_INTERVAL (30)  FAIL_THRESHOLD (3)  OK_THRESHOLD (3)  COOLDOWN (60)
  PING_TIMEOUT (5)     PROBE_HTTP_TIMEOUT (5)
  RESTORE_PRIMARY (1)  1=switch back when primary recovers, 0=stay on fallback
  ENSURE_PRIMARY (0)   1=select primary when no exit node is selected yet

Notification (environment only; NOT read from generated/failover.env):
  FAILOVER_NOTIFY_CMD  opt-in command run after a real switch attempt. Receives
                       FAILOVER_EVENT (switched|failed), FAILOVER_ROLE,
                       FAILOVER_LABEL, and FAILOVER_REASON in the environment.
                       Its exit status is ignored and cannot change the outcome;
                       keep it fast so it does not stall the controller.
EOF
}

usage_error() {
  printf 'error: %s\n' "$*" >&2
  usage >&2
  exit 2
}

show_version() {
  printf 'tailscale-ai-egress failover-exit-node.sh %s\n' "$VERSION"
}

note() { printf '%s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*" >&2; }
die() { printf '[FAIL] %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

cleanup() {
  release_lock
  local file
  if [ "${#tmp_files[@]}" -gt 0 ]; then
    for file in "${tmp_files[@]}"; do
      rm -f "$file"
    done
  fi
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

read_failover_env() {
  [ -n "$FAILOVER_ENV" ] && [ -r "$FAILOVER_ENV" ] || return 0
  local key value
  while IFS='=' read -r key value || [ -n "$key" ]; do
    case "$key" in
      ''|\#*) continue ;;
    esac
    value="${value%$'\r'}"
    case "$value" in
      '"'*'"') value="${value#\"}"; value="${value%\"}" ;;
      *) value="${value%%[[:space:]]\#*}" ;;
    esac
    while [ "${value%[[:space:]]}" != "$value" ]; do value="${value%[[:space:]]}"; done
    case "$key" in
      PRIMARY_EXIT_NODE) [ -n "$PRIMARY_EXIT_NODE" ] || PRIMARY_EXIT_NODE="$value" ;;
      FALLBACK_EXIT_NODE) [ -n "$FALLBACK_EXIT_NODE" ] || FALLBACK_EXIT_NODE="$value" ;;
      PROBE_TARGET) [ -n "$PROBE_TARGET" ] || PROBE_TARGET="$value" ;;
      CHECK_INTERVAL) [ -n "$CHECK_INTERVAL" ] || CHECK_INTERVAL="$value" ;;
      FAIL_THRESHOLD) [ -n "$FAIL_THRESHOLD" ] || FAIL_THRESHOLD="$value" ;;
      OK_THRESHOLD) [ -n "$OK_THRESHOLD" ] || OK_THRESHOLD="$value" ;;
      COOLDOWN) [ -n "$COOLDOWN" ] || COOLDOWN="$value" ;;
      PING_TIMEOUT) [ -n "$PING_TIMEOUT" ] || PING_TIMEOUT="$value" ;;
      PROBE_HTTP_TIMEOUT) [ -n "$PROBE_HTTP_TIMEOUT" ] || PROBE_HTTP_TIMEOUT="$value" ;;
      RESTORE_PRIMARY) [ -n "$RESTORE_PRIMARY" ] || RESTORE_PRIMARY="$value" ;;
      ENSURE_PRIMARY) [ -n "$ENSURE_PRIMARY" ] || ENSURE_PRIMARY="$value" ;;
      STATE_FILE) [ -n "$STATE_FILE" ] || STATE_FILE="$value" ;;
      READBACK_DELAY) [ -n "$READBACK_DELAY" ] || READBACK_DELAY="$value" ;;
    esac
  done < "$FAILOVER_ENV"
}

apply_defaults() {
  PROBE_TARGET="${PROBE_TARGET:-https://ipinfo.io}"
  CHECK_INTERVAL="${CHECK_INTERVAL:-30}"
  FAIL_THRESHOLD="${FAIL_THRESHOLD:-3}"
  OK_THRESHOLD="${OK_THRESHOLD:-3}"
  COOLDOWN="${COOLDOWN:-60}"
  PING_TIMEOUT="${PING_TIMEOUT:-5}"
  PROBE_HTTP_TIMEOUT="${PROBE_HTTP_TIMEOUT:-5}"
  RESTORE_PRIMARY="${RESTORE_PRIMARY:-1}"
  ENSURE_PRIMARY="${ENSURE_PRIMARY:-0}"
  STATE_FILE="${STATE_FILE:-$GENERATED_DIR/failover-state.json}"
  READBACK_DELAY="${READBACK_DELAY:-1}"
  LOCK_DIR="${LOCK_DIR:-$GENERATED_DIR/failover.lock.d}"
  LOCK_WAIT="${LOCK_WAIT:-30}"
}

# Upper bound for the seconds-valued config (intervals / timeouts / delays). A
# value above this is rejected up front so an absurd number (e.g. 120 digits)
# cannot reach `sleep`, which would fail and spin the watcher in a tight loop.
MAX_SLEEP_SECONDS=86400  # 1 day

# Numeric validators. The Python health engine re-validates every value it is
# handed (math.isfinite + range), so for values it consumes these are a fail-fast
# UX layer; but they are the ONLY guard for the shell-only values (CHECK_INTERVAL,
# READBACK_DELAY, LOCK_WAIT), so they must be complete. The number patterns reject
# empty, a bare/leading/trailing dot, non-numeric characters, and multiple dots --
# so ".", ".5", "5.", "nan", "inf", and "1e3" are all rejected -- and the awk
# range checks reject 0/negatives and values above MAX_SLEEP_SECONDS.
require_pos_number() {
  case "$2" in
    ''|.*|*.|*[!0-9.]*|*.*.*) die "$1 must be a positive number (got: '$2')" ;;
  esac
  awk -v v="$2" 'BEGIN { exit (v + 0 > 0) ? 0 : 1 }' || die "$1 must be greater than 0 (got: '$2')"
  awk -v v="$2" -v max="$MAX_SLEEP_SECONDS" 'BEGIN { exit (v + 0 <= max) ? 0 : 1 }' \
    || die "$1 must be at most $MAX_SLEEP_SECONDS seconds (got: '$2')"
}

require_nonneg_number() {
  case "$2" in
    ''|.*|*.|*[!0-9.]*|*.*.*) die "$1 must be a non-negative number (got: '$2')" ;;
  esac
  awk -v v="$2" -v max="$MAX_SLEEP_SECONDS" 'BEGIN { exit (v + 0 <= max) ? 0 : 1 }' \
    || die "$1 must be at most $MAX_SLEEP_SECONDS seconds (got: '$2')"
}

require_pos_int() {
  case "$2" in
    ''|*[!0-9]*) die "$1 must be an integer >= 1 (got: '$2')" ;;
  esac
  # The range check also rejects values too large for the shell's integer type,
  # which would otherwise make later `[ ... -ge ... ]` comparisons error out.
  [ "$2" -ge 1 ] 2>/dev/null || die "$1 must be an integer >= 1 within range (got: '$2')"
}

require_nonneg_int() {
  case "$2" in
    ''|*[!0-9]*) die "$1 must be a non-negative integer (got: '$2')" ;;
  esac
  # Reject values too large for the shell's integer type (e.g. an oversized
  # LOCK_WAIT) so the acquire loop's `-ge` comparison cannot error and spin.
  [ "$2" -ge 0 ] 2>/dev/null || die "$1 must be a non-negative integer within range (got: '$2')"
}

require_bool() {
  case "$2" in
    0|1) ;;
    *) die "$1 must be 0 or 1 (got: '$2')" ;;
  esac
}

validate_config() {
  require_pos_number CHECK_INTERVAL "$CHECK_INTERVAL"
  require_pos_number PING_TIMEOUT "$PING_TIMEOUT"
  require_pos_number PROBE_HTTP_TIMEOUT "$PROBE_HTTP_TIMEOUT"
  require_pos_int FAIL_THRESHOLD "$FAIL_THRESHOLD"
  require_pos_int OK_THRESHOLD "$OK_THRESHOLD"
  require_nonneg_number COOLDOWN "$COOLDOWN"
  require_nonneg_number READBACK_DELAY "$READBACK_DELAY"
  require_nonneg_int LOCK_WAIT "$LOCK_WAIT"
  require_bool RESTORE_PRIMARY "$RESTORE_PRIMARY"
  require_bool ENSURE_PRIMARY "$ENSURE_PRIMARY"
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --once) shift ;;
      --watch) WATCH=1; shift ;;
      --apply) APPLY=1; shift ;;
      --dry-run) DRY_RUN=1; shift ;;
      --egress) EGRESS=1; shift ;;
      --ensure-primary) ENSURE_PRIMARY=1; shift ;;
      --version) show_version; exit 0 ;;
      -h|--help) usage; exit 0 ;;
      *) usage_error "unknown argument: $1" ;;
    esac
  done
}

preflight() {
  have python3 || die "python3 is required to run the health engine."
  have tailscale || die "Tailscale CLI is not installed or not on PATH."
  [ -f "$HEALTH" ] || die "missing health engine: $HEALTH"
  [ -n "$PRIMARY_EXIT_NODE" ] || die "PRIMARY_EXIT_NODE is not set (configure $FAILOVER_ENV or the environment)."
  [ -n "$FALLBACK_EXIT_NODE" ] || die "FALLBACK_EXIT_NODE is not set (configure $FAILOVER_ENV or the environment)."
}

lock_age_seconds() {
  local now mtime
  now="$(date +%s)"
  case "$(uname -s)" in
    Darwin) mtime="$(stat -f %m "$LOCK_DIR" 2>/dev/null)" ;;
    *) mtime="$(stat -c %Y "$LOCK_DIR" 2>/dev/null)" ;;
  esac
  [ -n "$mtime" ] || return 1
  printf '%s\n' "$((now - mtime))"
}

proc_start_time() {
  # A stable per-process token so a recycled PID is not mistaken for the original
  # lock holder. Linux: starttime (jiffies since boot) from /proc, which is
  # immune to wall-clock changes. macOS / other: the rendered start time from ps.
  local pid="$1"
  if [ -r "/proc/$pid/stat" ]; then
    awk '{ rest=$0; sub(/^.*\) /, "", rest); split(rest, f, " "); print f[20] }' "/proc/$pid/stat" 2>/dev/null
    return 0
  fi
  ps -o lstart= -p "$pid" 2>/dev/null | tr -s '[:space:]' ' ' | sed 's/^ //; s/ $//'
}

is_stale_lock() {
  local pid recorded_start current_start age
  pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [ -n "$pid" ]; then
    if ! kill -0 "$pid" 2>/dev/null; then
      return 0  # pid recorded but the process is gone -> stale
    fi
    # The PID is live, but it may have been REUSED by an unrelated long-lived
    # process after our holder was SIGKILLed. Confirm identity via the recorded
    # process start time before treating the lock as still held.
    recorded_start="$(cat "$LOCK_DIR/start" 2>/dev/null || true)"
    current_start="$(proc_start_time "$pid")"
    if [ -n "$recorded_start" ] && [ -n "$current_start" ]; then
      [ "$recorded_start" = "$current_start" ] && return 1  # same process -> held
      return 0  # PID recycled by a different process -> stale
    fi
    # Identity unverifiable (no recorded or observable start time): fall back to
    # an age threshold so a recycled PID cannot block failover forever.
    age="$(lock_age_seconds)" || return 1
    [ -n "$age" ] && [ "$age" -gt 300 ]
    return
  fi
  # No pid recorded: fall back to an age threshold.
  age="$(lock_age_seconds)" || return 1
  [ -n "$age" ] && [ "$age" -gt 300 ]
}

acquire_lock() {
  mkdir -p "$GENERATED_DIR"
  local waited=0
  local stale_name=""
  while ! mkdir "$LOCK_DIR" 2>/dev/null; do
    if is_stale_lock; then
      warn "removing stale controller lock $LOCK_DIR"
      # Claim the stale lock atomically: rename it to a private, collision-proof
      # name before removing it. If two controllers both judge the lock stale,
      # only one rename can succeed; the loser's mv fails (the directory was
      # already renamed, or a fresh lock now sits there) and falls through to the
      # bounded wait below. That way it can neither delete a fresh lock the
      # winner just created (TOCTOU) nor spin without LOCK_WAIT accounting.
      stale_name="$LOCK_DIR.stale.$$.$RANDOM"
      if mv "$LOCK_DIR" "$stale_name" 2>/dev/null; then
        rm -rf "$stale_name"
        continue
      fi
    fi
    waited=$((waited + 1))
    if [ "$waited" -ge "$LOCK_WAIT" ]; then
      die "could not acquire controller lock $LOCK_DIR (another run in progress?)"
    fi
    sleep 1
  done
  printf '%s\n' "$$" >"$LOCK_DIR/pid" 2>/dev/null || true
  proc_start_time "$$" >"$LOCK_DIR/start" 2>/dev/null || true
  LOCK_HELD=1
}

release_lock() {
  [ "$LOCK_HELD" = "1" ] || return 0
  rm -rf "$LOCK_DIR" 2>/dev/null || true
  LOCK_HELD=0
}

run_verdict() {
  local args
  args=(
    verdict
    --state-file "$STATE_FILE"
    --primary "$PRIMARY_EXIT_NODE"
    --fallback "$FALLBACK_EXIT_NODE"
    --fail-threshold "$FAIL_THRESHOLD"
    --ok-threshold "$OK_THRESHOLD"
    --cooldown "$COOLDOWN"
    --restore-primary "$RESTORE_PRIMARY"
    --ping-timeout "$PING_TIMEOUT"
    --http-timeout "$PROBE_HTTP_TIMEOUT"
    --egress-url "$PROBE_TARGET"
  )
  if [ "$EGRESS" = "1" ]; then
    args+=(--egress)
  fi
  if [ "$ENSURE_PRIMARY" = "1" ]; then
    args+=(--ensure-primary)
  fi
  python3 "$HEALTH" "${args[@]}"
}

current_active_role() {
  python3 "$HEALTH" active-role --primary "$PRIMARY_EXIT_NODE" --fallback "$FALLBACK_EXIT_NODE"
}

record_switch() {
  python3 "$HEALTH" record-switch \
    --state-file "$STATE_FILE" \
    --primary "$PRIMARY_EXIT_NODE" --fallback "$FALLBACK_EXIT_NODE" \
    --role "$1" >/dev/null
}

run_tailscale_set() {
  local label="$1"
  if [ "$DRY_RUN" = "1" ]; then
    note "+ tailscale set --exit-node=$label"
    return 0
  fi
  case "$(uname -s)" in
    Darwin)
      tailscale set --exit-node="$label"
      ;;
    *)
      if [ "$(id -u)" -eq 0 ] || [ "$USE_SUDO" = "0" ]; then
        tailscale set --exit-node="$label"
      else
        sudo tailscale set --exit-node="$label"
      fi
      ;;
  esac
}

apply_switch() {
  local role="$1" label="$2"
  note "[apply] switching exit node -> $label (role=$role)"
  if ! run_tailscale_set "$label"; then
    warn "apply_failed: 'tailscale set --exit-node=$label' returned non-zero"
    return 1
  fi
  if [ "$DRY_RUN" = "1" ]; then
    note "[dry-run] printed switch command; skipping readback and state record"
    return 0
  fi
  if [ "$READBACK_DELAY" != "0" ]; then
    if ! sleep "$READBACK_DELAY"; then
      warn "apply_failed: readback settle sleep ('$READBACK_DELAY') failed; not recording an unverified switch"
      return 1
    fi
  fi
  local live
  live="$(current_active_role 2>/dev/null || printf 'error')"
  if [ "$live" = "$role" ]; then
    if ! record_switch "$role"; then
      # The exit node was switched, but persisting the new role + cooldown clock
      # failed (disk full, permissions, state lock). Report non-zero so callers
      # and monitoring do not treat this as a clean success.
      warn "switch_state_persist_failed: exit node is now $label (role=$role) but recording state failed; cooldown not saved"
      return 1
    fi
    note "[ok] exit node is now $label (role=$role); recorded switch"
    return 0
  fi
  warn "apply_failed: readback shows active role '$live', expected '$role'"
  return 1
}

notify_hook() {
  # Opt-in: run FAILOVER_NOTIFY_CMD after a real switch attempt, passing context
  # via the environment. It runs synchronously but its exit status is discarded
  # (`|| true`), so a broken hook can never change the switch outcome. Keep the
  # command fast: a hook that hangs will stall the controller loop.
  [ -n "$FAILOVER_NOTIFY_CMD" ] || return 0
  FAILOVER_EVENT="$1" FAILOVER_ROLE="$2" FAILOVER_LABEL="$3" FAILOVER_REASON="$4" \
    sh -c "$FAILOVER_NOTIFY_CMD" || true
}

run_cycle() {
  acquire_lock
  local decision action reason target_role target_label event active_role k v rc
  action=""; reason=""; target_role=""; target_label=""; event=""; active_role=""
  if ! decision="$(run_verdict)"; then
    warn "health engine verdict failed; skipping this cycle"
    release_lock
    return 1
  fi
  while IFS='=' read -r k v; do
    case "$k" in
      action) action="$v" ;;
      reason) reason="$v" ;;
      target_role) target_role="$v" ;;
      target_label) target_label="$v" ;;
      event) event="$v" ;;
      active_role) active_role="$v" ;;
    esac
  done <<<"$decision"

  note "[decision] active=$active_role action=$action reason=$reason target=$target_label event=$event"
  if [ "$event" = "primary_recovered" ] && [ "$reason" = "restore_primary_disabled" ]; then
    note "[info] primary recovered but RESTORE_PRIMARY=0; staying on fallback (no switch)"
  fi

  if [ "$action" = "none" ]; then
    case "$reason" in
      live_status_unavailable|live_status_incomplete|candidates_not_distinct|backend_not_running)
        # A live-state problem, not a healthy "nothing to do": surface it so
        # --once can alert (non-zero) while --watch logs and keeps looping.
        warn "live state problem: $reason (cannot evaluate failover)"
        release_lock
        return 3
        ;;
    esac
    release_lock
    return 0
  fi
  if [ "$APPLY" != "1" ]; then
    note "[observe] proposed: $action -> $target_label (reason=$reason); re-run with --apply to act"
    release_lock
    return 0
  fi

  apply_switch "$target_role" "$target_label"
  rc=$?
  release_lock
  # Fire the opt-in notification after releasing the lock (so a slow hook cannot
  # block another controller) and only for real switch attempts, not dry runs.
  if [ "$DRY_RUN" != "1" ]; then
    if [ "$rc" -eq 0 ]; then
      notify_hook "switched" "$target_role" "$target_label" "$reason"
    else
      notify_hook "failed" "$target_role" "$target_label" "$reason"
    fi
  fi
  return "$rc"
}

main() {
  parse_args "$@"
  read_failover_env
  apply_defaults
  validate_config
  preflight
  if [ "$WATCH" = "1" ]; then
    note "[watch] exit-node failover controller started (interval=${CHECK_INTERVAL}s apply=$APPLY)"
    while true; do
      run_cycle || true
      sleep "$CHECK_INTERVAL" || die "sleep for CHECK_INTERVAL='$CHECK_INTERVAL' failed; refusing to busy-loop"
    done
  else
    run_cycle
  fi
}

main "$@"
