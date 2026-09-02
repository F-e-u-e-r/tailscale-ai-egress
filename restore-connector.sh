#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="$(cat "$SCRIPT_DIR/VERSION" 2>/dev/null || true)"
VERSION="${VERSION:-1.3.0}"

# Shared internal library (source-only; see docs/design/shared-shell-library.md).
COMMON_LIB="$SCRIPT_DIR/scripts/lib/common.sh"
if [ ! -r "$COMMON_LIB" ]; then
  printf 'error: missing shared library %s\n' "$COMMON_LIB" >&2
  exit 1
fi
# shellcheck source=scripts/lib/common.sh
. "$COMMON_LIB"

REGION="${REGION:-}"
REGION_LOWER=""
GENERATED_DIR="${GENERATED_DIR:-$SCRIPT_DIR/generated}"
CONNECTOR_TAG="${CONNECTOR_TAG:-}"
CONNECTOR_HOSTNAME="${CONNECTOR_HOSTNAME:-}"
REQUESTED_REGION="$REGION"
REQUESTED_CONNECTOR_TAG="$CONNECTOR_TAG"
REQUESTED_CONNECTOR_HOSTNAME="$CONNECTOR_HOSTNAME"
DRY_RUN="${DRY_RUN:-0}"
USE_SUDO="${AI_EGRESS_USE_SUDO:-1}"
FORCE_RESET=0
RESET_ACK="${RESTORE_RESET_ACK:-0}"
STATUS_JSON=""
IDENTITY_SOURCE=""
PERSISTED_IDENTITY_PRESENT=0
PERSISTED_REGION=""
PERSISTED_CONNECTOR_TAG=""
PERSISTED_CONNECTOR_HOSTNAME=""
STATUS_CONNECTOR_TAG_COUNT=0
STATUS_CONNECTOR_TAG=""
STATUS_CONNECTOR_HOSTNAME=""

usage() {
  cat <<'EOF'
Usage: ./restore-connector.sh [--dry-run] [--force-reset] [--yes]

Restore the intended App Connector mode after using exit-node fallback.

Default restore is conservative: it disables exit-node advertising, then tries
to re-enable app connector advertising with tailscale set. It does not reset
hostname, tags, DNS, route acceptance, or other local Tailscale preferences.

Options:
  --dry-run      Print commands without changing the host.
  --force-reset  Use tailscale up --reset with the full connector flags.
  --yes          Confirm the --force-reset preference reset warning.
  --version      Print the tailscale-ai-egress version and exit.
  -h, --help     Show this help.

Useful environment variables:
  REGION=us
  CONNECTOR_TAG=tag:ai-egress-us
  CONNECTOR_HOSTNAME=ai-egress-us-01
  GENERATED_DIR=/path/to/output
  RESTORE_RESET_ACK=1
EOF
}

usage_error() {
  printf 'error: %s\n' "$*" >&2
  usage >&2
  exit 2
}

show_version() {
  printf 'tailscale-ai-egress restore-connector.sh %s\n' "$VERSION"
}

note() {
  printf '%s\n' "$*"
}

warn() {
  printf '[WARN] %s\n' "$*" >&2
}

die() {
  printf '[FAIL] %s\n' "$*" >&2
  exit 1
}

have() {
  command -v "$1" >/dev/null 2>&1
}

normalize_region() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

valid_region() {
  local region="$1"
  case "$region" in
    ""|*[!a-z0-9-]*|-*|*-|*--*)
      return 1
      ;;
    *)
      return 0
      ;;
  esac
}

validate_connector_tag() {
  case "$CONNECTOR_TAG" in
    tag:*) ;;
    *) die "CONNECTOR_TAG must start with 'tag:' (got: $CONNECTOR_TAG)" ;;
  esac

  local tag_name="${CONNECTOR_TAG#tag:}"
  case "$tag_name" in
    ""|*[!a-z0-9-]*|-*|*-|*--*)
      die "CONNECTOR_TAG must be tag:<lowercase-alphanumeric-hyphens> (got: $CONNECTOR_TAG)"
      ;;
  esac
}

validate_connector_hostname() {
  case "$CONNECTOR_HOSTNAME" in
    ""|*[!a-z0-9-]*|-*|*-|*--*)
      die "CONNECTOR_HOSTNAME must contain only lowercase letters, numbers, and single hyphens, and cannot start or end with a hyphen (got: $CONNECTOR_HOSTNAME)"
      ;;
  esac
}

derive_identity_from_region() {
  if [ -z "$REGION" ]; then
    return 0
  fi

  REGION_LOWER="$(normalize_region "$REGION")"
  if ! valid_region "$REGION_LOWER"; then
    die "REGION must contain only letters, numbers, and single hyphens, and cannot start or end with a hyphen (got: $REGION)"
  fi
  [ -n "$CONNECTOR_TAG" ] || CONNECTOR_TAG="tag:ai-egress-$REGION_LOWER"
  [ -n "$CONNECTOR_HOSTNAME" ] || CONNECTOR_HOSTNAME="ai-egress-$REGION_LOWER-01"
}

read_persisted_identity() {
  local identity_file="$GENERATED_DIR/connector-identity.env"
  local key value
  [ -r "$identity_file" ] || return 0
  PERSISTED_IDENTITY_PRESENT=1

  while IFS='=' read -r key value; do
    case "$key" in
      REGION)
        PERSISTED_REGION="$value"
        ;;
      CONNECTOR_TAG)
        PERSISTED_CONNECTOR_TAG="$value"
        ;;
      CONNECTOR_HOSTNAME)
        PERSISTED_CONNECTOR_HOSTNAME="$value"
        ;;
    esac
  done <"$identity_file"
}

read_status_identity() {
  local fields rest
  [ -n "$STATUS_JSON" ] && have python3 || return 0

  if ! fields="$(STATUS_JSON_PAYLOAD="$STATUS_JSON" python3 - 2>/dev/null <<'PY'
import json
import os

data = json.loads(os.environ["STATUS_JSON_PAYLOAD"])
self_node = data.get("Self") or {}
tags = []
for value in self_node.get("Tags") or []:
    tag = str(value)
    if tag.startswith("tag:ai-egress-") and tag not in tags:
        tags.append(tag)

hostname = ""
for candidate in (
    self_node.get("HostName"),
    self_node.get("Hostname"),
    self_node.get("Name"),
    str(self_node.get("DNSName", "")).split(".")[0],
):
    if candidate:
        hostname = str(candidate)
        break

tag = tags[0] if len(tags) == 1 else ""
print(f"{len(tags)}|{tag}|{hostname}")
PY
  )"; then
    return 1
  fi
  [ -n "$fields" ] || return 1
  STATUS_CONNECTOR_TAG_COUNT="${fields%%|*}"
  rest="${fields#*|}"
  STATUS_CONNECTOR_TAG="${rest%%|*}"
  STATUS_CONNECTOR_HOSTNAME="${rest#*|}"
}

resolve_connector_identity() {
  CONNECTOR_TAG="$REQUESTED_CONNECTOR_TAG"
  CONNECTOR_HOSTNAME="$REQUESTED_CONNECTOR_HOSTNAME"
  REGION="$REQUESTED_REGION"
  REGION_LOWER=""
  IDENTITY_SOURCE=""
  PERSISTED_IDENTITY_PRESENT=0
  PERSISTED_REGION=""
  PERSISTED_CONNECTOR_TAG=""
  PERSISTED_CONNECTOR_HOSTNAME=""
  STATUS_CONNECTOR_TAG_COUNT=0
  STATUS_CONNECTOR_TAG=""
  STATUS_CONNECTOR_HOSTNAME=""

  if ! read_status_identity; then
    die "Could not parse Tailscale status JSON. Refusing to run tailscale up --reset."
  fi
  if [ "$STATUS_CONNECTOR_TAG_COUNT" -gt 1 ]; then
    die "Tailscale status contains multiple tag:ai-egress-* tags on this node. Remove the ambiguous tags before restoring connector identity."
  fi

  if [ -n "$REQUESTED_CONNECTOR_TAG" ] || [ -n "$REQUESTED_CONNECTOR_HOSTNAME" ]; then
    if [ -z "$REQUESTED_CONNECTOR_TAG" ] || [ -z "$REQUESTED_CONNECTOR_HOSTNAME" ]; then
      die "Explicit connector identity is incomplete. Set both CONNECTOR_TAG and CONNECTOR_HOSTNAME, or set neither and use REGION."
    fi
    validate_connector_tag
    validate_connector_hostname
    IDENTITY_SOURCE="environment"
    return 0
  fi

  if [ -n "$REGION" ]; then
    derive_identity_from_region
    validate_connector_tag
    validate_connector_hostname
    IDENTITY_SOURCE="region"
    return 0
  fi

  read_persisted_identity
  if [ "$PERSISTED_IDENTITY_PRESENT" = "1" ]; then
    if [ -z "$PERSISTED_CONNECTOR_TAG" ] || [ -z "$PERSISTED_CONNECTOR_HOSTNAME" ]; then
      die "Persisted connector identity is incomplete. Rerun ./bootstrap.sh or delete $GENERATED_DIR/connector-identity.env so status detection can be used."
    fi
    CONNECTOR_TAG="$PERSISTED_CONNECTOR_TAG"
    CONNECTOR_HOSTNAME="$PERSISTED_CONNECTOR_HOSTNAME"
    REGION="$PERSISTED_REGION"
    validate_connector_tag
    validate_connector_hostname
    if [ -n "$STATUS_CONNECTOR_TAG" ] && [ "$STATUS_CONNECTOR_TAG" != "$CONNECTOR_TAG" ]; then
      die "Persisted connector tag $CONNECTOR_TAG conflicts with current Tailscale status tag $STATUS_CONNECTOR_TAG. Refusing to reset with stale identity."
    fi
    if [ -n "$STATUS_CONNECTOR_HOSTNAME" ] && [ "$STATUS_CONNECTOR_HOSTNAME" != "$CONNECTOR_HOSTNAME" ]; then
      die "Persisted connector hostname $CONNECTOR_HOSTNAME conflicts with current Tailscale hostname $STATUS_CONNECTOR_HOSTNAME. Refusing to reset with stale identity."
    fi
    IDENTITY_SOURCE="persisted file"
    return 0
  fi

  if [ "$STATUS_CONNECTOR_TAG_COUNT" = "1" ]; then
    [ -n "$STATUS_CONNECTOR_HOSTNAME" ] || die "Tailscale status has a connector tag but no usable hostname. Set both CONNECTOR_TAG and CONNECTOR_HOSTNAME explicitly."
    CONNECTOR_TAG="$STATUS_CONNECTOR_TAG"
    CONNECTOR_HOSTNAME="$STATUS_CONNECTOR_HOSTNAME"
    validate_connector_tag
    validate_connector_hostname
    IDENTITY_SOURCE="Tailscale status"
    return 0
  fi

  CONNECTOR_TAG=""
  CONNECTOR_HOSTNAME=""
}

run_root_output() {
  # Only call this from non-dry-run branches that need to inspect command output.
  if [ "$(id -u)" -eq 0 ] || [ "$USE_SUDO" = "0" ]; then
    "$@"
  else
    sudo "$@"
  fi
}

read_line() {
  local prompt="$1"
  local answer=""
  if [ -t 0 ]; then
    read -r -p "$prompt" answer
  elif ( : </dev/tty ) 2>/dev/null; then
    # `read -p` writes the prompt to stderr; when stdin is not a tty we redirect
    # from /dev/tty, so print the prompt to /dev/tty explicitly (mirrors
    # rollback.sh) instead of relying on -p, which would be swallowed here.
    printf '%s' "$prompt" >/dev/tty
    if ! read -r answer </dev/tty; then
      return 1
    fi
  else
    return 1
  fi
  printf '%s' "$answer"
}

yes_no() {
  local prompt="$1"
  local answer
  answer="$(read_line "$prompt [y/N] " || true)"
  case "$answer" in
    y|Y|yes|YES|Yes) return 0 ;;
    *) return 1 ;;
  esac
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --dry-run)
        DRY_RUN=1
        shift
        ;;
      --force-reset)
        FORCE_RESET=1
        shift
        ;;
      --yes)
        RESET_ACK=1
        shift
        ;;
      --version)
        show_version
        exit 0
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        usage_error "unknown argument: $1"
        ;;
    esac
  done
}

preflight() {
  have tailscale || die "Tailscale CLI is not installed."
  if ! STATUS_JSON="$(tailscale status --json 2>/dev/null)"; then
    die "Tailscale is not running or this host is not logged in."
  fi
  if [ "$FORCE_RESET" = "1" ]; then
    resolve_connector_identity
  fi
}

connector_set_unsupported() {
  grep -Eqi 'unknown flag|flag provided but not defined|unrecognized|not defined|unsupported'
}

default_restore() {
  ai_egress_run_root tailscale set --advertise-exit-node=false

  if [ "$DRY_RUN" = "1" ]; then
    ai_egress_run_root tailscale set --advertise-connector
  else
    local output
    if ! output="$(run_root_output tailscale set --advertise-connector 2>&1)"; then
      if printf '%s\n' "$output" | connector_set_unsupported; then
        warn "This Tailscale client does not support 'tailscale set --advertise-connector'."
        warn "Use './restore-connector.sh --force-reset' only if you intentionally want to repair connector flags with 'tailscale up --reset'."
        warn "That repair path can clear local preferences such as accept-routes and accept-dns."
        return 1
      fi
      printf '%s\n' "$output" >&2
      return 1
    fi
  fi

  note "[OK] Requested connector advertising and disabled exit-node advertising."
  if [ "$DRY_RUN" != "1" ]; then
    verify_restore
  else
    note "Dry run only; restore verification was not run."
  fi
}

confirm_force_reset() {
  warn "Force reset will run 'tailscale up --reset' with the full connector flags."
  warn "This clears local Tailscale preferences such as accept-routes, accept-dns, and manually added flags."

  if [ "$RESET_ACK" = "1" ]; then
    note "[OK] RESTORE_RESET_ACK/--yes set; continuing with explicit reset repair."
    return 0
  fi

  yes_no "Reset local Tailscale preferences and restore bootstrap connector flags?" || die "Cancelled; connector reset was not run."
}

force_reset_restore() {
  [ -n "$CONNECTOR_TAG" ] || die "Could not identify a connector tag. Set CONNECTOR_TAG or REGION, rerun ./bootstrap.sh to create generated/connector-identity.env, or make sure this node has a tag:ai-egress-* tag."
  [ -n "$CONNECTOR_HOSTNAME" ] || die "Could not identify a connector hostname. Set CONNECTOR_HOSTNAME or REGION, rerun ./bootstrap.sh to create generated/connector-identity.env, or make sure Tailscale status reports this host's name."
  note "Using connector identity from $IDENTITY_SOURCE: $CONNECTOR_TAG / $CONNECTOR_HOSTNAME"
  confirm_force_reset
  ai_egress_run_root tailscale up \
    --reset \
    --hostname="$CONNECTOR_HOSTNAME" \
    --advertise-connector \
    --advertise-exit-node=false \
    --advertise-tags="$CONNECTOR_TAG"
  note "[OK] Full connector flags were restored with tailscale up --reset."
  if [ "$DRY_RUN" != "1" ]; then
    verify_restore
  else
    note "Dry run only; restore verification was not run."
  fi
}

verify_restore() {
  have python3 || {
    warn "python3 is unavailable; hostname/tag verification skipped."
    return 0
  }
  if ! STATUS_JSON="$(tailscale status --json 2>/dev/null)"; then
    warn "Could not verify Tailscale status after restore."
    return 0
  fi
  STATUS_CONNECTOR_TAG_COUNT=0
  STATUS_CONNECTOR_TAG=""
  STATUS_CONNECTOR_HOSTNAME=""
  if ! read_status_identity; then
    warn "Could not parse Tailscale status after restore; hostname/tag verification was skipped."
    return 0
  fi

  if [ "$STATUS_CONNECTOR_TAG_COUNT" -gt 1 ]; then
    warn "Multiple tag:ai-egress-* tags are visible; connector tag verification is ambiguous."
  elif [ "$STATUS_CONNECTOR_TAG_COUNT" = "1" ]; then
    note "[OK] Connector tag $STATUS_CONNECTOR_TAG is visible."
  else
    warn "No tag:ai-egress-* connector tag is visible. Default restore cannot repair tags; use --force-reset or fix tags manually."
  fi

  if [ -n "$STATUS_CONNECTOR_HOSTNAME" ]; then
    note "[OK] Connector hostname $STATUS_CONNECTOR_HOSTNAME is visible."
  else
    warn "No connector hostname is visible. Default restore does not change hostname; use --force-reset if repair is needed."
  fi
}

main() {
  parse_args "$@"
  preflight
  if [ "$FORCE_RESET" = "1" ]; then
    force_reset_restore
  else
    default_restore
  fi
}

main "$@"
