#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="$(cat "$ROOT_DIR/VERSION" 2>/dev/null || true)"
VERSION="${VERSION:-1.1.1}"

HEALTH="$ROOT_DIR/scripts/health_check.py"

# Config precedence: explicit environment > generated/failover.env > default.
GENERATED_DIR="${GENERATED_DIR:-$ROOT_DIR/generated}"
FAILOVER_ENV="${FAILOVER_ENV:-$GENERATED_DIR/failover.env}"
PRIMARY_CONNECTOR="${PRIMARY_CONNECTOR:-}"
FALLBACK_CONNECTOR="${FALLBACK_CONNECTOR:-}"
CHECK_INTERVAL="${CHECK_INTERVAL:-}"
PING_TIMEOUT="${PING_TIMEOUT:-}"
REQUIRE_ROUTES="${REQUIRE_ROUTES:-}"

WATCH=0
JSON=0
PROM_TEXTFILE=""

usage() {
  cat <<'EOF'
Usage: ./monitor-connectors.sh [--once|--watch] [--json]

Read-only health monitor for an App Connector high-availability pair. Tailscale
itself performs connector failover (oldest connector = primary, oldest-first,
all plans). This tool only OBSERVES: it never switches anything.

It reports, for the primary and fallback connector, online + tailnet
reachability, and -- only if a Tailscale API token is configured -- the
device.created ordering (to confirm the intended primary is the oldest). With
no token it prints "ordering=unavailable" and still runs the health checks.

Each connector also carries read-only metrics (tx/rx byte counters cumulative
since tailscaled started, last-handshake age, and a derived direct/derp path):
a "metrics" object per connector in --json, and an append-only "[metrics]" line
in text mode. Metrics never affect the exit status. See
docs/design/metrics-collection.md.

Exit status: 0 if both connectors are online and reachable, 1 if degraded.

Options:
  --once       Run a single check (default). Useful from cron for alerting.
  --watch      Loop forever, checking every CHECK_INTERVAL seconds.
  --json       Emit a JSON report instead of text.
  --prometheus-textfile <path>
               Write a node_exporter textfile (per-connector gauges) to <path>
               (must end in .prom) instead of printing the normal report, then
               exit; with --watch, rewrite it every CHECK_INTERVAL. The file is
               written atomically. Exit status reflects write success, not health
               (health is the ai_egress_overall_healthy gauge). Not combinable
               with --json. Node-level tailscaled_* metrics are separately
               available via `tailscale metrics print`.
  --version    Print the tailscale-ai-egress version and exit.
  -h, --help   Show this help.

Configuration (environment or generated/failover.env):
  PRIMARY_CONNECTOR, FALLBACK_CONNECTOR   connector hostname / MagicDNS / IP
  CHECK_INTERVAL (30)   PING_TIMEOUT (5)   REQUIRE_ROUTES (1)

Environment only (NOT read from generated/failover.env):
  TAILSCALE_API_KEY (optional)            enables device.created ordering
                                          Export it in the environment (e.g. via
                                          systemd EnvironmentFile=); this script
                                          does not parse it from failover.env.
EOF
}

usage_error() {
  printf 'error: %s\n' "$*" >&2
  usage >&2
  exit 2
}

show_version() {
  printf 'tailscale-ai-egress monitor-connectors.sh %s\n' "$VERSION"
}

note() { printf '%s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*" >&2; }
die() { printf '[FAIL] %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

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
      PRIMARY_CONNECTOR) [ -n "$PRIMARY_CONNECTOR" ] || PRIMARY_CONNECTOR="$value" ;;
      FALLBACK_CONNECTOR) [ -n "$FALLBACK_CONNECTOR" ] || FALLBACK_CONNECTOR="$value" ;;
      CHECK_INTERVAL) [ -n "$CHECK_INTERVAL" ] || CHECK_INTERVAL="$value" ;;
      PING_TIMEOUT) [ -n "$PING_TIMEOUT" ] || PING_TIMEOUT="$value" ;;
      REQUIRE_ROUTES) [ -n "$REQUIRE_ROUTES" ] || REQUIRE_ROUTES="$value" ;;
    esac
  done < "$FAILOVER_ENV"
}

apply_defaults() {
  CHECK_INTERVAL="${CHECK_INTERVAL:-30}"
  PING_TIMEOUT="${PING_TIMEOUT:-5}"
  REQUIRE_ROUTES="${REQUIRE_ROUTES:-1}"
}

# Upper bound for the seconds-valued config so an absurd CHECK_INTERVAL cannot
# reach `sleep` and spin the watcher.
MAX_SLEEP_SECONDS=86400  # 1 day

# Rejects empty, a bare/leading/trailing dot, non-numeric characters, and
# multiple dots (so ".", ".5", "5.", "nan", "inf", "1e3" are all rejected), plus
# 0/negatives and values above MAX_SLEEP_SECONDS. The Python health engine
# re-validates the values it receives.
require_pos_number() {
  case "$2" in
    ''|.*|*.|*[!0-9.]*|*.*.*) die "$1 must be a positive number (got: '$2')" ;;
  esac
  awk -v v="$2" 'BEGIN { exit (v + 0 > 0) ? 0 : 1 }' || die "$1 must be greater than 0 (got: '$2')"
  awk -v v="$2" -v max="$MAX_SLEEP_SECONDS" 'BEGIN { exit (v + 0 <= max) ? 0 : 1 }' \
    || die "$1 must be at most $MAX_SLEEP_SECONDS seconds (got: '$2')"
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
  require_bool REQUIRE_ROUTES "$REQUIRE_ROUTES"
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --once) shift ;;
      --watch) WATCH=1; shift ;;
      --json) JSON=1; shift ;;
      --prometheus-textfile)
        shift
        [ "$#" -ge 1 ] || usage_error "--prometheus-textfile requires a <path> argument"
        [ -n "$1" ] || usage_error "--prometheus-textfile requires a non-empty path"
        case "$1" in -*) usage_error "--prometheus-textfile requires a path, got option '$1'" ;; esac
        PROM_TEXTFILE="$1"; shift ;;
      --version) show_version; exit 0 ;;
      -h|--help) usage; exit 0 ;;
      *) usage_error "unknown argument: $1" ;;
    esac
  done
  if [ -n "$PROM_TEXTFILE" ] && [ "$JSON" = "1" ]; then
    usage_error "--json cannot be combined with --prometheus-textfile"
  fi
}

preflight() {
  have python3 || die "python3 is required to run the health engine."
  have tailscale || die "Tailscale CLI is not installed or not on PATH."
  [ -f "$HEALTH" ] || die "missing health engine: $HEALTH"
  [ -n "$PRIMARY_CONNECTOR" ] || die "PRIMARY_CONNECTOR is not set (configure $FAILOVER_ENV or the environment)."
  [ -n "$FALLBACK_CONNECTOR" ] || die "FALLBACK_CONNECTOR is not set (configure $FAILOVER_ENV or the environment)."
}

run_check() {
  local args
  args=(
    connectors
    --primary "$PRIMARY_CONNECTOR"
    --fallback "$FALLBACK_CONNECTOR"
    --ping-timeout "$PING_TIMEOUT"
    --require-routes "$REQUIRE_ROUTES"
  )
  if [ "$JSON" = "1" ]; then
    args+=(--json)
  fi
  python3 "$HEALTH" "${args[@]}"
}

write_prometheus_textfile() {
  # Delegate the whole validated document AND the atomic write to the Python
  # engine (which owns the .prom validation, fchmod 0644, fsync, and os.replace).
  # Returns the engine's status: 0 on a successful write (even when the pair is
  # degraded -- health is carried by the ai_egress_overall_healthy gauge, not the
  # exit code), nonzero on a write/validation failure. On failure the existing
  # file is left untouched and Python's stderr is surfaced via warn.
  local err rc
  # `2>&1 >/dev/null` (in THIS order) captures Python's stderr into `err` and
  # discards its stdout; do NOT "simplify" it to `>/dev/null 2>&1`, which would
  # capture nothing. `rc` is Python's exit status.
  err="$(python3 "$HEALTH" connectors \
    --primary "$PRIMARY_CONNECTOR" --fallback "$FALLBACK_CONNECTOR" \
    --ping-timeout "$PING_TIMEOUT" --require-routes "$REQUIRE_ROUTES" \
    --prometheus --output "$PROM_TEXTFILE" 2>&1 >/dev/null)"
  rc=$?
  if [ "$rc" -ne 0 ]; then
    warn "prometheus textfile write failed (rc=$rc): ${err:-unknown error}; kept any existing $PROM_TEXTFILE"
  else
    note "[prometheus] wrote $PROM_TEXTFILE"
  fi
  return "$rc"
}

main() {
  parse_args "$@"
  read_failover_env
  apply_defaults
  validate_config
  preflight
  if [ -n "$PROM_TEXTFILE" ]; then
    # Textfile-only mode: write the node_exporter .prom (Python owns generation +
    # atomic write); the normal text/JSON report is NOT also printed (one pass).
    if [ "$WATCH" = "1" ]; then
      note "[watch] prometheus textfile writer started (interval=${CHECK_INTERVAL}s -> $PROM_TEXTFILE)"
      while true; do
        write_prometheus_textfile || true  # keep looping; existing file retained on failure
        sleep "$CHECK_INTERVAL" || die "sleep for CHECK_INTERVAL='$CHECK_INTERVAL' failed; refusing to busy-loop"
      done
    else
      write_prometheus_textfile
      return $?
    fi
  fi
  if [ "$WATCH" = "1" ]; then
    note "[watch] connector monitor started (interval=${CHECK_INTERVAL}s)"
    while true; do
      run_check || true
      sleep "$CHECK_INTERVAL" || die "sleep for CHECK_INTERVAL='$CHECK_INTERVAL' failed; refusing to busy-loop"
    done
  else
    run_check
  fi
}

main "$@"
