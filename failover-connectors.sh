#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="$(cat "$ROOT_DIR/VERSION" 2>/dev/null || true)"
VERSION="${VERSION:-1.3.0}"

# Initialised early so the cleanup trap is safe even on Bash 3.2 early exits.
tmp_files=()
LOCK_HELD=0
LOCK_DIR=""
LOCK_WAIT="${LOCK_WAIT:-}"

POLICY_TOOL="${POLICY_TOOL:-$ROOT_DIR/scripts/policy_tool.py}"

# Config precedence: explicit environment > generated/failover.env > built-in default.
GENERATED_DIR="${GENERATED_DIR:-$ROOT_DIR/generated}"
FAILOVER_ENV="${FAILOVER_ENV:-$GENERATED_DIR/failover.env}"
PRIMARY_CONNECTOR_TAG="${PRIMARY_CONNECTOR_TAG:-}"
FALLBACK_CONNECTOR_TAG="${FALLBACK_CONNECTOR_TAG:-}"
CONNECTOR_SWITCH_COOLDOWN="${CONNECTOR_SWITCH_COOLDOWN:-}"
# Environment-only passthroughs (never parsed from failover.env):
CONNECTOR_NAME="${CONNECTOR_NAME:-}"
FAILOVER_NOTIFY_CMD="${FAILOVER_NOTIFY_CMD:-}"
# Internal overrides (environment only; test seams and advanced use):
CONNECTOR_SWITCH_STATE_FILE="${CONNECTOR_SWITCH_STATE_FILE:-}"
CONNECTOR_SWITCH_LOCK_DIR="${CONNECTOR_SWITCH_LOCK_DIR:-}"

TARGET_TAG=""
APPLY=0

usage() {
  cat <<'EOF'
Usage: ./failover-connectors.sh [--to tag:<pool> [--apply]]

Model B (distinct-tag) active connector switch: an operator-invoked, one-shot,
auditable forced selection of which connector pool serves the AI domain set.
See docs/Failover.md "Advanced Mode" for the full workflow.

Modes:
  (no arguments)        Report: both pools' node liveness, the managed entry's
                        live connectors value, declaration readiness, and
                        state-file drift. Mutates nothing, writes nothing.
  --to tag:<pool>       Plan only: run the fail-closed preconditions, generate
                        an auditable plan bundle via `policy_tool.py
                        connector-plan --switch-to` (with a compare-and-swap
                        on the value read here), print its diff, and stop.
  --to tag:<pool> --apply
                        Additionally re-check target-pool liveness immediately
                        before `apply-plan` (which keeps its interactive
                        `APPLY <plan-id>` confirmation), then verify by
                        reading the policy back. State is recorded only after
                        a verified switch.
  --version             Print the tailscale-ai-egress version and exit.
  -h, --help            Show this help.

Configuration (environment or generated/failover.env):
  PRIMARY_CONNECTOR_TAG / FALLBACK_CONNECTOR_TAG   the two pool tags
  CONNECTOR_SWITCH_COOLDOWN (600)  advisory warning window after a switch,
                        integer seconds in [0, 86400]; 0 disables the warning.

Environment only (not read from generated/failover.env):
  CONNECTOR_NAME        managed app-connector entry name (default
                        AI-Egress-<REGION>, resolved by policy_tool.py).
  TAILSCALE_API_KEY or TAILSCALE_OAUTH_CLIENT_ID+TAILSCALE_OAUTH_CLIENT_SECRET
                        policy credential (required for a switch; without it
                        the report degrades to status-only).
  FAILOVER_NOTIFY_CMD   opt-in hook run after a completed switch
                        (FAILOVER_EVENT=connector-switch) or a persistent
                        readback failure (connector-switch-readback-failed);
                        receives FAILOVER_ROLE, FAILOVER_LABEL,
                        FAILOVER_REASON, FAILOVER_PLAN_ID. Its exit status is
                        ignored and cannot change the switch outcome.

Notes: the switch lock is held through the interactive APPLY confirmation, so
a second invocation waiting on the lock during a pending confirmation is
expected, not a hang. There is deliberately no --force and no --watch.
EOF
}

usage_error() {
  printf 'error: %s\n' "$*" >&2
  usage >&2
  exit 2
}

show_version() {
  printf 'tailscale-ai-egress failover-connectors.sh %s\n' "$VERSION"
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

new_tmp_file() {
  # Observation temps come from the system tempdir, NEVER generated/: a
  # cold-start report must leave the generated/ tree byte-untouched.
  # NOTE: callers run this in a command substitution (a subshell), so the
  # REGISTRATION for the EXIT-trap cleanup must happen in the caller
  # (tmp_files+=) — an append here would be lost with the subshell.
  mktemp "${TMPDIR:-/tmp}/ai-egress-connector.XXXXXX" || die "mktemp failed"
}

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
      PRIMARY_CONNECTOR_TAG) [ -n "$PRIMARY_CONNECTOR_TAG" ] || PRIMARY_CONNECTOR_TAG="$value" ;;
      FALLBACK_CONNECTOR_TAG) [ -n "$FALLBACK_CONNECTOR_TAG" ] || FALLBACK_CONNECTOR_TAG="$value" ;;
      CONNECTOR_SWITCH_COOLDOWN) [ -n "$CONNECTOR_SWITCH_COOLDOWN" ] || CONNECTOR_SWITCH_COOLDOWN="$value" ;;
    esac
  done < "$FAILOVER_ENV"
}

apply_defaults() {
  CONNECTOR_SWITCH_COOLDOWN="${CONNECTOR_SWITCH_COOLDOWN:-600}"
  CONNECTOR_SWITCH_STATE_FILE="${CONNECTOR_SWITCH_STATE_FILE:-$GENERATED_DIR/connector-switch-state.json}"
  CONNECTOR_SWITCH_LOCK_DIR="${CONNECTOR_SWITCH_LOCK_DIR:-$GENERATED_DIR/connector-switch.lock.d}"
  LOCK_DIR="$CONNECTOR_SWITCH_LOCK_DIR"
  LOCK_WAIT="${LOCK_WAIT:-30}"
}

MAX_COOLDOWN_SECONDS=86400  # 1 day

require_cooldown() {
  # Integer seconds in [0, 86400]; 0 disables the warning. Rejected before any
  # probing or switching (the docs' contract).
  case "$CONNECTOR_SWITCH_COOLDOWN" in
    ''|*[!0-9]*) die "CONNECTOR_SWITCH_COOLDOWN must be an integer in [0, $MAX_COOLDOWN_SECONDS] (got: '$CONNECTOR_SWITCH_COOLDOWN')" ;;
  esac
  [ "$CONNECTOR_SWITCH_COOLDOWN" -le "$MAX_COOLDOWN_SECONDS" ] 2>/dev/null \
    || die "CONNECTOR_SWITCH_COOLDOWN must be at most $MAX_COOLDOWN_SECONDS seconds (got: '$CONNECTOR_SWITCH_COOLDOWN')"
}

require_nonneg_int() {
  case "$2" in
    ''|*[!0-9]*) die "$1 must be a non-negative integer (got: '$2')" ;;
  esac
  [ "$2" -ge 0 ] 2>/dev/null || die "$1 must be a non-negative integer within range (got: '$2')"
}

valid_tag() {
  # Mirrors policy_tool.py's CONNECTOR_TAG_RE: ^tag:[a-z0-9]+(-[a-z0-9]+)*$
  case "$1" in
    tag:*) ;;
    *) return 1 ;;
  esac
  printf '%s\n' "${1#tag:}" | LC_ALL=C grep -Eq '^[a-z0-9]+(-[a-z0-9]+)*$'
}

validate_switch_config() {
  [ -n "$PRIMARY_CONNECTOR_TAG" ] || die "PRIMARY_CONNECTOR_TAG is not set (configure $FAILOVER_ENV or the environment; see docs/Configuration.md)"
  [ -n "$FALLBACK_CONNECTOR_TAG" ] || die "FALLBACK_CONNECTOR_TAG is not set (configure $FAILOVER_ENV or the environment; see docs/Configuration.md)"
  valid_tag "$PRIMARY_CONNECTOR_TAG" || die "PRIMARY_CONNECTOR_TAG is not a valid tag (got: '$PRIMARY_CONNECTOR_TAG')"
  valid_tag "$FALLBACK_CONNECTOR_TAG" || die "FALLBACK_CONNECTOR_TAG is not a valid tag (got: '$FALLBACK_CONNECTOR_TAG')"
  [ "$PRIMARY_CONNECTOR_TAG" != "$FALLBACK_CONNECTOR_TAG" ] \
    || die "PRIMARY_CONNECTOR_TAG and FALLBACK_CONNECTOR_TAG must be distinct (both: '$PRIMARY_CONNECTOR_TAG')"
  case "$TARGET_TAG" in
    "$PRIMARY_CONNECTOR_TAG"|"$FALLBACK_CONNECTOR_TAG") ;;
    *) die "--to must name one of the configured pool pair ($PRIMARY_CONNECTOR_TAG | $FALLBACK_CONNECTOR_TAG); got: '$TARGET_TAG'" ;;
  esac
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --to)
        [ -n "$TARGET_TAG" ] && usage_error "duplicate --to"
        [ "$#" -ge 2 ] || usage_error "--to requires a value (tag:<pool>)"
        case "$2" in
          ''|-*) usage_error "--to requires a non-empty value (tag:<pool>); got: '$2'" ;;
        esac
        TARGET_TAG="$2"
        shift 2
        ;;
      --apply) APPLY=1; shift ;;
      --version) show_version; exit 0 ;;
      -h|--help) usage; exit 0 ;;
      *) usage_error "unknown argument: $1" ;;
    esac
  done
  if [ "$APPLY" = "1" ] && [ -z "$TARGET_TAG" ]; then
    usage_error "--apply requires --to tag:<pool>"
  fi
}

credential_present() {
  # Exactly get_api_token's set: an API key, or the OAuth client pair.
  # TAILSCALE_AUTHKEY is a NODE key, never a policy credential.
  [ -n "${TAILSCALE_API_KEY:-}" ] && return 0
  [ -n "${TAILSCALE_OAUTH_CLIENT_ID:-}" ] && [ -n "${TAILSCALE_OAUTH_CLIENT_SECRET:-}" ] && return 0
  return 1
}

# ----- lock (copied from failover-exit-node.sh; promotion to
# scripts/lib/common.sh is tracked by the shared-shell-library migration) -----

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
      return 0
    fi
    recorded_start="$(cat "$LOCK_DIR/start" 2>/dev/null || true)"
    current_start="$(proc_start_time "$pid")"
    if [ -n "$recorded_start" ] && [ -n "$current_start" ]; then
      [ "$recorded_start" = "$current_start" ] && return 1
      return 0
    fi
    age="$(lock_age_seconds)" || return 1
    [ -n "$age" ] && [ "$age" -gt 300 ]
    return
  fi
  age="$(lock_age_seconds)" || return 1
  [ -n "$age" ] && [ "$age" -gt 300 ]
}

acquire_lock() {
  mkdir -p "$GENERATED_DIR"
  local waited=0
  local stale_name=""
  while ! mkdir "$LOCK_DIR" 2>/dev/null; do
    if is_stale_lock; then
      warn "removing stale connector-switch lock $LOCK_DIR"
      stale_name="$LOCK_DIR.stale.$$.$RANDOM"
      if mv "$LOCK_DIR" "$stale_name" 2>/dev/null; then
        rm -rf "$stale_name"
        continue
      fi
    fi
    waited=$((waited + 1))
    if [ "$waited" -ge "$LOCK_WAIT" ]; then
      die "could not acquire connector-switch lock $LOCK_DIR (another switch in progress?)"
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

# ----- observation helpers (every inline python takes DATA via argv file
# paths, never stdin: `python3 -` consumes stdin for the program source) -----

STATUS_FILE=""
capture_status() {
  # One `tailscale status --json` capture per call site; apply-time re-checks
  # call this again so liveness is never a cached plan-time result.
  STATUS_FILE="$(new_tmp_file)" || return 1
  tmp_files+=("$STATUS_FILE")
  if ! tailscale status --json >"$STATUS_FILE" 2>/dev/null; then
    return 1
  fi
  [ -s "$STATUS_FILE" ]
}

# Emits KEY=VALUE lines: backend_state, online_primary, online_fallback,
# dual_tagged (comma-joined hostnames). Exits non-zero on unparsable JSON.
parse_liveness() {
  python3 - "$STATUS_FILE" "$PRIMARY_CONNECTOR_TAG" "$FALLBACK_CONNECTOR_TAG" <<'PY'
import json, sys
path, primary, fallback = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path, "r", encoding="utf-8") as handle:
        status = json.load(handle)
except (OSError, ValueError):
    sys.exit(1)
if not isinstance(status, dict):
    sys.exit(1)
nodes = []
self_node = status.get("Self")
if isinstance(self_node, dict):
    nodes.append(self_node)
peers = status.get("Peer")
if isinstance(peers, dict):
    nodes.extend(p for p in peers.values() if isinstance(p, dict))
elif isinstance(peers, list):
    nodes.extend(p for p in peers if isinstance(p, dict))

def tags_of(node):
    tags = node.get("Tags")
    return [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else []

online = {primary: 0, fallback: 0}
dual = []
for node in nodes:
    tags = tags_of(node)
    if node.get("Online") is True:
        for tag in (primary, fallback):
            if tag and tag in tags:
                online[tag] += 1
    if primary and fallback and primary in tags and fallback in tags:
        name = node.get("HostName")
        dual.append(name if isinstance(name, str) and name else "<unnamed>")

print(f"backend_state={status.get('BackendState', '')}")
print(f"online_primary={online[primary] if primary else ''}")
print(f"online_fallback={online[fallback] if fallback else ''}")
print(f"dual_tagged={','.join(dual)}")
PY
}

BACKEND_STATE=""
ONLINE_PRIMARY=""
ONLINE_FALLBACK=""
DUAL_TAGGED=""
load_liveness() {
  BACKEND_STATE=""; ONLINE_PRIMARY=""; ONLINE_FALLBACK=""; DUAL_TAGGED=""
  have tailscale || return 1
  capture_status || return 1
  local out k v
  out="$(parse_liveness)" || return 1
  while IFS='=' read -r k v; do
    case "$k" in
      backend_state) BACKEND_STATE="$v" ;;
      online_primary) ONLINE_PRIMARY="$v" ;;
      online_fallback) ONLINE_FALLBACK="$v" ;;
      dual_tagged) DUAL_TAGGED="$v" ;;
    esac
  done <<EOF
$out
EOF
  [ "$BACKEND_STATE" = "Running" ]
}

warn_dual_tagged() {
  [ -n "$DUAL_TAGGED" ] || return 0
  warn "node(s) carrying BOTH pool tags: $DUAL_TAGGED — a switch cannot evacuate them; resolve dual-tagged nodes promptly (this is a warning, not a refusal)"
}

online_count_for() {
  case "$1" in
    "$PRIMARY_CONNECTOR_TAG") printf '%s\n' "$ONLINE_PRIMARY" ;;
    "$FALLBACK_CONNECTOR_TAG") printf '%s\n' "$ONLINE_FALLBACK" ;;
    *) printf '%s\n' "" ;;
  esac
}

# Runs `policy_tool.py connector-state`; stdout lands in the given file.
run_connector_state() {
  local out_file="$1"
  local args
  args=(connector-state)
  [ -n "$CONNECTOR_NAME" ] && args+=(--connector-name "$CONNECTOR_NAME")
  # One --tag per SET, NON-EMPTY key; an unset key's --tag is omitted entirely.
  [ -n "$PRIMARY_CONNECTOR_TAG" ] && args+=(--tag "$PRIMARY_CONNECTOR_TAG")
  [ -n "$FALLBACK_CONNECTOR_TAG" ] && args+=(--tag "$FALLBACK_CONNECTOR_TAG")
  python3 "$POLICY_TOOL" "${args[@]}" >"$out_file" 2>"$out_file.err"
}

# Emits KEY=VALUE lines from a connector-state JSON document:
# entry_count, connectors_json (compact; the literal `null` when absent),
# connectors_is_single_list (1/0), single_value (the sole element when
# connectors is exactly a one-element string list, else empty),
# ready_<role>=1/0/'' and readiness_error_<role>.
parse_connector_state() {
  python3 - "$1" "$PRIMARY_CONNECTOR_TAG" "$FALLBACK_CONNECTOR_TAG" <<'PY'
import json, sys
path, primary, fallback = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path, "r", encoding="utf-8") as handle:
        doc = json.load(handle)
except (OSError, ValueError):
    sys.exit(1)
if not isinstance(doc, dict):
    sys.exit(1)
connectors = doc.get("connectors")
single = ""
is_single = 0
if isinstance(connectors, list) and len(connectors) == 1 and isinstance(connectors[0], str):
    is_single = 1
    single = connectors[0]
print(f"entry_count={doc.get('entry_count', '')}")
print(f"connectors_json={json.dumps(connectors, separators=(',', ':'))}")
print(f"connectors_is_single_list={is_single}")
print(f"single_value={single}")
readiness = doc.get("readiness")
readiness = readiness if isinstance(readiness, dict) else {}
for role, tag in (("primary", primary), ("fallback", fallback)):
    entry = readiness.get(tag) if tag else None
    if isinstance(entry, dict):
        print(f"ready_{role}={1 if entry.get('ready') is True else 0}")
        error = entry.get("error")
        print(f"readiness_error_{role}={error if isinstance(error, str) else ''}")
    else:
        print(f"ready_{role}=")
        print(f"readiness_error_{role}=")
PY
}

ENTRY_COUNT=""
CONNECTORS_JSON=""
CONNECTORS_IS_SINGLE=""
SINGLE_VALUE=""
READY_PRIMARY=""
READY_FALLBACK=""
READINESS_ERROR_PRIMARY=""
READINESS_ERROR_FALLBACK=""
load_connector_state() {
  local out_file parse_out k v
  if [ ! -f "$POLICY_TOOL" ]; then
    CONNECTOR_STATE_ERROR="missing policy tool: $POLICY_TOOL"
    return 1
  fi
  out_file="$(new_tmp_file)" || return 1
  tmp_files+=("$out_file" "$out_file.err")
  if ! run_connector_state "$out_file"; then
    CONNECTOR_STATE_ERROR="$(head -3 "$out_file.err" 2>/dev/null)"
    return 1
  fi
  parse_out="$(parse_connector_state "$out_file")" || { CONNECTOR_STATE_ERROR="connector-state output did not parse"; return 1; }
  while IFS='=' read -r k v; do
    case "$k" in
      entry_count) ENTRY_COUNT="$v" ;;
      connectors_json) CONNECTORS_JSON="$v" ;;
      connectors_is_single_list) CONNECTORS_IS_SINGLE="$v" ;;
      single_value) SINGLE_VALUE="$v" ;;
      ready_primary) READY_PRIMARY="$v" ;;
      ready_fallback) READY_FALLBACK="$v" ;;
      readiness_error_primary) READINESS_ERROR_PRIMARY="$v" ;;
      readiness_error_fallback) READINESS_ERROR_FALLBACK="$v" ;;
    esac
  done <<EOF
$parse_out
EOF
  return 0
}
CONNECTOR_STATE_ERROR=""

# ----- state file (advisory; policy is the source of truth) -----

# Emits KEY=VALUE lines: state=absent|malformed|ok, active_tag,
# last_switch_at, last_plan_id, in_cooldown=0/1 (cooldown arithmetic in
# python — never GNU/BSD `date` parsing).
read_switch_state() {
  python3 - "$CONNECTOR_SWITCH_STATE_FILE" "$CONNECTOR_SWITCH_COOLDOWN" <<'PY'
import datetime
import json
import sys
path, cooldown_raw = sys.argv[1], sys.argv[2]
try:
    cooldown = int(cooldown_raw)
except ValueError:
    cooldown = 0
try:
    with open(path, "r", encoding="utf-8") as handle:
        state = json.load(handle)
except FileNotFoundError:
    print("state=absent")
    sys.exit(0)
except (OSError, ValueError):
    print("state=malformed")
    sys.exit(0)
if not isinstance(state, dict) or state.get("schema_version") != 1:
    print("state=malformed")
    sys.exit(0)
active = state.get("active_tag")
switched_at = state.get("last_switch_at")
plan_id = state.get("last_plan_id")
previous = state.get("previous_connectors")
if not (
    isinstance(active, str)
    and isinstance(switched_at, str)
    and isinstance(plan_id, str)
    and isinstance(previous, list)
):
    print("state=malformed")
    sys.exit(0)
in_cooldown = 0
if cooldown > 0:
    try:
        then = datetime.datetime.strptime(switched_at, "%Y-%m-%dT%H:%M:%SZ")
        age = (datetime.datetime.utcnow() - then).total_seconds()
        if 0 <= age < cooldown:
            in_cooldown = 1
    except ValueError:
        print("state=malformed")
        sys.exit(0)
print("state=ok")
print(f"active_tag={active}")
print(f"last_switch_at={switched_at}")
print(f"last_plan_id={plan_id}")
print(f"in_cooldown={in_cooldown}")
PY
}

STATE_STATUS=""
STATE_ACTIVE_TAG=""
STATE_LAST_SWITCH_AT=""
STATE_LAST_PLAN_ID=""
STATE_IN_COOLDOWN=""
load_switch_state() {
  STATE_STATUS=""; STATE_ACTIVE_TAG=""; STATE_LAST_SWITCH_AT=""; STATE_LAST_PLAN_ID=""; STATE_IN_COOLDOWN=""
  local out k v
  out="$(read_switch_state)" || { STATE_STATUS="malformed"; return 0; }
  while IFS='=' read -r k v; do
    case "$k" in
      state) STATE_STATUS="$v" ;;
      active_tag) STATE_ACTIVE_TAG="$v" ;;
      last_switch_at) STATE_LAST_SWITCH_AT="$v" ;;
      last_plan_id) STATE_LAST_PLAN_ID="$v" ;;
      in_cooldown) STATE_IN_COOLDOWN="$v" ;;
    esac
  done <<EOF
$out
EOF
  if [ "$STATE_STATUS" = "malformed" ]; then
    warn "state file $CONNECTOR_SWITCH_STATE_FILE is malformed; ignoring it (advisory only — drift and cooldown checks skipped)"
  fi
  return 0
}

# Atomic write, ONLY after apply success + verified readback. The temp file is
# a sibling of the state file (same directory) so the rename is atomic; this
# is the one deliberate write path in the tool.
write_switch_state() {
  local plan_id="$1"
  python3 - "$CONNECTOR_SWITCH_STATE_FILE" "$TARGET_TAG" "$MANIFEST_FROM_JSON" "$plan_id" <<'PY'
import datetime
import json
import os
import sys
import tempfile
path, active_tag, previous_json, plan_id = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
state = {
    "schema_version": 1,
    "active_tag": active_tag,
    "previous_connectors": json.loads(previous_json),
    "last_switch_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    "last_plan_id": plan_id,
}
directory = os.path.dirname(path) or "."
os.makedirs(directory, exist_ok=True)
fd, tmp_path = tempfile.mkstemp(prefix=".connector-switch-state.", dir=directory)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
except OSError as exc:
    try:
        os.unlink(tmp_path)
    except OSError:
        pass
    print(f"state write failed: {exc}", file=sys.stderr)
    sys.exit(1)
PY
}

notify_hook() {
  # Opt-in, environment-only; exit status discarded so a broken hook can never
  # change the switch outcome. Fired only AFTER the lock is released.
  [ -n "$FAILOVER_NOTIFY_CMD" ] || return 0
  FAILOVER_EVENT="$1" FAILOVER_ROLE="$2" FAILOVER_LABEL="$3" FAILOVER_REASON="$4" FAILOVER_PLAN_ID="$5" \
    sh -c "$FAILOVER_NOTIFY_CMD" || true
}

role_of() {
  if [ "$1" = "$PRIMARY_CONNECTOR_TAG" ]; then printf 'primary\n'; else printf 'fallback\n'; fi
}

# ----- report mode -----

report_mode() {
  local liveness_ok=0 policy_ok=0
  note "connector-switch report (Model B) — policy is the source of truth; the state file is advisory"
  if load_liveness; then
    liveness_ok=1
    if [ -n "$PRIMARY_CONNECTOR_TAG" ]; then
      note "pool $PRIMARY_CONNECTOR_TAG (primary): online nodes: $ONLINE_PRIMARY"
    else
      note "pool primary: not-configured (PRIMARY_CONNECTOR_TAG unset)"
    fi
    if [ -n "$FALLBACK_CONNECTOR_TAG" ]; then
      note "pool $FALLBACK_CONNECTOR_TAG (fallback): online nodes: $ONLINE_FALLBACK"
    else
      note "pool fallback: not-configured (FALLBACK_CONNECTOR_TAG unset)"
    fi
    warn_dual_tagged
  else
    note "liveness: unavailable (tailscale status failed, unparsable, or backend not Running${BACKEND_STATE:+ — BackendState=$BACKEND_STATE})"
  fi

  if ! credential_present; then
    note "active pool: unavailable (no policy credential; set TAILSCALE_API_KEY or the OAuth pair to enable)"
    note "declarations: unavailable"
    note "drift: unavailable"
  elif load_connector_state; then
    policy_ok=1
    note "managed entries matching connector name: $ENTRY_COUNT"
    note "live connectors value: $CONNECTORS_JSON"
    [ -n "$PRIMARY_CONNECTOR_TAG" ] && {
      if [ "$READY_PRIMARY" = "1" ]; then note "declaration $PRIMARY_CONNECTOR_TAG: ready"; else note "declaration $PRIMARY_CONNECTOR_TAG: NOT ready${READINESS_ERROR_PRIMARY:+ — $READINESS_ERROR_PRIMARY}"; fi
    }
    [ -n "$FALLBACK_CONNECTOR_TAG" ] && {
      if [ "$READY_FALLBACK" = "1" ]; then note "declaration $FALLBACK_CONNECTOR_TAG: ready"; else note "declaration $FALLBACK_CONNECTOR_TAG: NOT ready${READINESS_ERROR_FALLBACK:+ — $READINESS_ERROR_FALLBACK}"; fi
    }
    load_switch_state
    case "$STATE_STATUS" in
      absent) note "state file: absent (cold start — active pool derived from live policy)" ;;
      ok)
        note "state file: last switch to $STATE_ACTIVE_TAG at $STATE_LAST_SWITCH_AT (plan $STATE_LAST_PLAN_ID)"
        if [ "$CONNECTORS_IS_SINGLE" != "1" ]; then
          note "drift: not assessable — the live connectors value above is not a single pool tag (state comparison does not apply; live policy wins)"
        elif [ "$STATE_ACTIVE_TAG" != "$SINGLE_VALUE" ]; then
          note "drift: state file says $STATE_ACTIVE_TAG but live policy says $SINGLE_VALUE (state is advisory; live policy wins)"
        else
          note "drift: none"
        fi
        ;;
    esac
  else
    note "active pool: unavailable (connector-state failed${CONNECTOR_STATE_ERROR:+: $CONNECTOR_STATE_ERROR})"
    note "declarations: unavailable"
    note "drift: unavailable"
  fi

  if [ "$liveness_ok" = "0" ] && [ "$policy_ok" = "0" ]; then
    die "neither the status view nor the policy view is available"
  fi
  return 0
}

# ----- switch flow -----

PREVIOUS_CONNECTORS_JSON=""
MANIFEST_FROM_JSON=""
PLAN_DIR=""
PLAN_ID=""

run_switch_plan() {
  # Preflight reads are advisory; the planner re-fetches and re-validates
  # everything against its own snapshot, and --expected-from makes a policy
  # change between our read and its fetch a refusal (TOCTOU, two witnesses).
  local plan_out
  plan_out="$(new_tmp_file)" || die "mktemp failed"
  tmp_files+=("$plan_out")
  if ! python3 "$POLICY_TOOL" connector-plan \
      --switch-to "$TARGET_TAG" \
      ${CONNECTOR_NAME:+--connector-name "$CONNECTOR_NAME"} \
      --plans-dir "$GENERATED_DIR/policy-plans" \
      --expected-from "$PREVIOUS_CONNECTORS_JSON" >"$plan_out" 2>&1; then
    cat "$plan_out" >&2
    die "connector-plan refused the switch (see output above)"
  fi
  # Validate EVERYTHING before echoing any success output: bundle location,
  # manifest identity, and a successfully captured diff.
  local dirs
  dirs="$(grep -c '^Plan directory: ' "$plan_out" || true)"
  [ "$dirs" = "1" ] || die "expected exactly one 'Plan directory:' line from connector-plan (got $dirs)"
  PLAN_DIR="$(sed -n 's/^Plan directory: //p' "$plan_out" | head -1)"
  [ -d "$PLAN_DIR" ] || die "planner-reported bundle directory does not exist: $PLAN_DIR"
  local manifest_check
  manifest_check="$(python3 - "$PLAN_DIR/manifest.json" "$TARGET_TAG" <<'PY'
import json, sys
path, target = sys.argv[1], sys.argv[2]
try:
    with open(path, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
except (OSError, ValueError):
    print("unreadable")
    sys.exit(0)
if manifest.get("operation") != "switch-connectors" or manifest.get("to") != target:
    print("mismatch")
    sys.exit(0)
plan_id = manifest.get("plan_id")
if not isinstance(plan_id, str) or not plan_id or "from" not in manifest:
    print("unreadable")
    sys.exit(0)
print(plan_id)
# The manifest's verbatim `from` is the planner's own authoritative pre-switch
# snapshot; state's previous_connectors comes from HERE, never from the
# script's preflight read (they must agree under --expected-from anyway).
print(json.dumps(manifest["from"], separators=(",", ":")))
PY
)" || die "could not validate the bundle manifest"
  case "$manifest_check" in
    unreadable) die "bundle manifest $PLAN_DIR/manifest.json is unreadable or invalid" ;;
    mismatch) die "bundle manifest is not a switch-connectors operation targeting $TARGET_TAG; refusing" ;;
  esac
  PLAN_ID="$(printf '%s\n' "$manifest_check" | sed -n 1p)"
  MANIFEST_FROM_JSON="$(printf '%s\n' "$manifest_check" | sed -n 2p)"
  [ -n "$MANIFEST_FROM_JSON" ] || die "bundle manifest is missing its verbatim 'from' value"
  # The diff must be a REGULAR file whose content is captured successfully
  # before any success output — a directory named diff.patch or an I/O error
  # is a fail-closed refusal, never a silent continue.
  [ -f "$PLAN_DIR/diff.patch" ] || die "bundle diff.patch is missing or not a regular file at $PLAN_DIR/diff.patch"
  local diff_content
  diff_content="$(cat "$PLAN_DIR/diff.patch")" || die "could not read bundle diff.patch at $PLAN_DIR/diff.patch"
  cat "$plan_out"
  note "--- plan diff ($PLAN_DIR/diff.patch) ---"
  printf '%s\n' "$diff_content"
  note "--- end plan diff ---"
}

readback_matches_target() {
  local out_file
  out_file="$(new_tmp_file)" || return 1
  tmp_files+=("$out_file" "$out_file.err")
  run_connector_state "$out_file" || return 1
  python3 - "$out_file" "$TARGET_TAG" <<'PY'
import json, sys
path, target = sys.argv[1], sys.argv[2]
try:
    with open(path, "r", encoding="utf-8") as handle:
        doc = json.load(handle)
except (OSError, ValueError):
    sys.exit(1)
sys.exit(0 if doc.get("connectors") == [target] else 1)
PY
}

switch_mode() {
  validate_switch_config
  credential_present || die "a policy credential is required for a switch (set TAILSCALE_API_KEY or the OAuth pair); the no-argument report still works without one"
  acquire_lock

  load_liveness || { release_lock; die "tailscale status unavailable, unparsable, or backend not Running — refusing to switch (fail-closed)"; }
  warn_dual_tagged
  local target_online
  target_online="$(online_count_for "$TARGET_TAG")"
  if ! { [ -n "$target_online" ] && [ "$target_online" -ge 1 ] 2>/dev/null; }; then
    release_lock
    die "target pool $TARGET_TAG has no online tagged node — nowhere safe to go"
  fi

  load_connector_state || { release_lock; die "could not read the live policy (connector-state failed${CONNECTOR_STATE_ERROR:+: $CONNECTOR_STATE_ERROR})"; }
  [ "$ENTRY_COUNT" = "1" ] \
    || { release_lock; die "expected exactly one managed app-connector entry (got: ${ENTRY_COUNT:-unknown}); fix --connector-name/CONNECTOR_NAME or the policy"; }
  if [ "$CONNECTORS_IS_SINGLE" != "1" ] \
     || { [ "$SINGLE_VALUE" != "$PRIMARY_CONNECTOR_TAG" ] && [ "$SINGLE_VALUE" != "$FALLBACK_CONNECTOR_TAG" ]; }; then
    release_lock
    warn "live connectors value is $CONNECTORS_JSON — not exactly one configured pool tag (drift)"
    warn "recovery: review the live policy, then either reconcile with the raw planner:"
    warn "  python3 scripts/policy_tool.py connector-plan --switch-to $TARGET_TAG"
    warn "  (its one-element bundle diff IS the reconciliation; review before apply-plan)"
    warn "or edit the connectors list in the Admin Console."
    die "refusing the scripted switch until the drift is reconciled"
  fi
  if [ "$SINGLE_VALUE" = "$TARGET_TAG" ]; then
    release_lock
    die "$TARGET_TAG is already the sole active connector pool; nothing to switch"
  fi
  PREVIOUS_CONNECTORS_JSON="$CONNECTORS_JSON"

  load_switch_state
  if [ "$STATE_STATUS" = "ok" ] && [ "$STATE_IN_COOLDOWN" = "1" ]; then
    warn "cooldown: the last switch (to $STATE_ACTIVE_TAG, plan $STATE_LAST_PLAN_ID) was at $STATE_LAST_SWITCH_AT, within CONNECTOR_SWITCH_COOLDOWN=${CONNECTOR_SWITCH_COOLDOWN}s"
    warn "this is an anti-fat-finger warning, not a lockout; the APPLY <plan-id> confirmation still stands"
  fi

  run_switch_plan

  if [ "$APPLY" != "1" ]; then
    note "[plan-only] bundle generated; the tailnet was not written. Re-run with --apply, or: python3 scripts/policy_tool.py apply-plan $PLAN_DIR"
    release_lock
    return 0
  fi

  # Apply-time liveness re-check: a FRESH status read immediately before
  # apply-plan; never the cached plan-time result.
  load_liveness || { release_lock; die "tailscale status unavailable at apply time — refusing (fail-closed)"; }
  target_online="$(online_count_for "$TARGET_TAG")"
  if ! { [ -n "$target_online" ] && [ "$target_online" -ge 1 ] 2>/dev/null; }; then
    release_lock
    die "target pool $TARGET_TAG lost its last online node between planning and apply — refusing"
  fi

  # apply-plan keeps its interactive exact `APPLY <plan-id>` confirmation; this
  # script never passes --yes, so the scripted apply is interactive by design.
  # A NON-ZERO apply-plan is AMBIGUOUS, not proof of no-write: the frozen tool
  # can successfully POST the policy and still exit non-zero (its documented
  # applied-but-manifest-update-failed arm). The READBACK below is therefore
  # the authority on whether the switch landed, in both arms.
  local apply_rc=0
  python3 "$POLICY_TOOL" apply-plan "$PLAN_DIR" || apply_rc=$?

  # Readback: read once; on mismatch, settle briefly and read ONCE more.
  local readback_ok=0
  if readback_matches_target; then
    readback_ok=1
  else
    sleep 2
    if readback_matches_target; then
      readback_ok=1
    fi
  fi

  if [ "$readback_ok" != "1" ]; then
    release_lock
    if [ "$apply_rc" -ne 0 ]; then
      warn "apply-plan exited non-zero AND the readback did not show the target after two reads —"
      warn "most likely the apply itself failed (a 412 means the policy changed after planning:"
      warn "regenerate by re-running this switch). If apply-plan reported the plan APPLIED but"
      warn "could not update its manifest, re-check reality with the no-argument report before acting."
      warn "No state was recorded and nothing further was written."
      exit 1
    fi
    warn "readback did not confirm connectors == [\"$TARGET_TAG\"] after two reads"
    warn "NOTHING further was written automatically and no state was recorded."
    warn "recovery paths:"
    warn "  1) compensating switch-back: ./failover-connectors.sh --to <previous-pool> --apply"
    warn "  2) python3 scripts/policy_tool.py restore-plan $PLAN_DIR"
    warn "     (CAUTION: restore-plan rewrites the ENTIRE policy from the captured snapshot;"
    warn "      any policy edit made after that snapshot is lost)"
    notify_hook "connector-switch-readback-failed" "$(role_of "$TARGET_TAG")" "$TARGET_TAG" "readback-mismatch" "$PLAN_ID"
    exit 1
  fi

  if [ "$apply_rc" -ne 0 ]; then
    # apply-plan exited non-zero but the readback VERIFIED the switch landed:
    # the applied-but-manifest-update-failed arm. The switch is real, so state
    # is recorded and the truthful connector-switch notify fires — but the
    # exit stays non-zero to surface the apply-plan error (the bundle's
    # manifest was likely not marked applied; do not re-run apply-plan on it).
    if write_switch_state "$PLAN_ID"; then
      warn "switch to $TARGET_TAG VERIFIED by readback, but apply-plan exited $apply_rc (its bundle manifest was likely not updated — do not re-run apply-plan on this bundle)"
    else
      warn "switch to $TARGET_TAG VERIFIED by readback, but apply-plan exited $apply_rc AND recording $CONNECTOR_SWITCH_STATE_FILE failed"
    fi
    release_lock
    notify_hook "connector-switch" "$(role_of "$TARGET_TAG")" "$TARGET_TAG" "operator-switch" "$PLAN_ID"
    exit 1
  fi

  if ! write_switch_state "$PLAN_ID"; then
    release_lock
    warn "switch to $TARGET_TAG APPLIED AND VERIFIED, but recording $CONNECTOR_SWITCH_STATE_FILE failed — the cooldown clock and previous_connectors were NOT saved"
    notify_hook "connector-switch" "$(role_of "$TARGET_TAG")" "$TARGET_TAG" "operator-switch" "$PLAN_ID"
    exit 1
  fi
  note "[ok] active connector pool is now $TARGET_TAG (plan $PLAN_ID); state recorded"
  release_lock
  notify_hook "connector-switch" "$(role_of "$TARGET_TAG")" "$TARGET_TAG" "operator-switch" "$PLAN_ID"
  return 0
}

preflight_report() {
  # Report degrades PER SOURCE: a missing tailscale CLI only makes the
  # liveness view unavailable, and a missing policy tool only the policy
  # view; python3 is the one hard requirement (every parser runs on it).
  have python3 || die "python3 is required."
}

preflight_switch() {
  have python3 || die "python3 is required."
  have tailscale || die "Tailscale CLI is not installed or not on PATH."
  [ -f "$POLICY_TOOL" ] || die "missing policy tool: $POLICY_TOOL"
}

main() {
  parse_args "$@"
  read_failover_env
  apply_defaults
  require_cooldown
  require_nonneg_int LOCK_WAIT "$LOCK_WAIT"
  if [ -n "$TARGET_TAG" ]; then
    preflight_switch
    switch_mode
  else
    preflight_report
    report_mode
  fi
}

main "$@"
