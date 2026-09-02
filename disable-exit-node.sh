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
REQUESTED_REGION="$REGION"
REQUESTED_CONNECTOR_TAG="$CONNECTOR_TAG"
DRY_RUN="${DRY_RUN:-0}"
USE_SUDO="${AI_EGRESS_USE_SUDO:-1}"
STATUS_JSON=""
PERSISTED_IDENTITY_PRESENT=0
PERSISTED_REGION=""
PERSISTED_CONNECTOR_TAG=""
STATUS_CONNECTOR_TAG_COUNT=0
STATUS_CONNECTOR_TAG=""

usage() {
  cat <<'EOF'
Usage: ./disable-exit-node.sh [--dry-run]

Stop advertising exit-node capability on this host while leaving Tailscale and
the App Connector setup in place.

Options:
  --dry-run  Print the Tailscale command without changing the host.
  --version  Print the tailscale-ai-egress version and exit.
  -h, --help Show this help.
EOF
}

usage_error() {
  printf 'error: %s\n' "$*" >&2
  usage >&2
  exit 2
}

show_version() {
  printf 'tailscale-ai-egress disable-exit-node.sh %s\n' "$VERSION"
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

derive_identity_from_region() {
  if [ -z "$REGION" ]; then
    return 0
  fi

  REGION_LOWER="$(normalize_region "$REGION")"
  if ! valid_region "$REGION_LOWER"; then
    die "REGION must contain only letters, numbers, and single hyphens, and cannot start or end with a hyphen (got: $REGION)"
  fi
  [ -n "$CONNECTOR_TAG" ] || CONNECTOR_TAG="tag:ai-egress-$REGION_LOWER"
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
    esac
  done <"$identity_file"
}

read_status_connector_tag() {
  local fields
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

selected = tags[0] if len(tags) == 1 else ""
print(f"{len(tags)}|{selected}")
PY
  )"; then
    return 1
  fi
  [ -n "$fields" ] || return 1
  STATUS_CONNECTOR_TAG_COUNT="${fields%%|*}"
  STATUS_CONNECTOR_TAG="${fields#*|}"
}

resolve_connector_identity() {
  CONNECTOR_TAG="$REQUESTED_CONNECTOR_TAG"
  REGION="$REQUESTED_REGION"
  REGION_LOWER=""
  PERSISTED_IDENTITY_PRESENT=0
  PERSISTED_REGION=""
  PERSISTED_CONNECTOR_TAG=""
  STATUS_CONNECTOR_TAG_COUNT=0
  STATUS_CONNECTOR_TAG=""

  if ! read_status_connector_tag; then
    die "Could not parse Tailscale status JSON. Refusing to disable exit-node fallback."
  fi
  if [ "$STATUS_CONNECTOR_TAG_COUNT" -gt 1 ]; then
    die "Tailscale status contains multiple tag:ai-egress-* tags on this node. Remove the ambiguous tags before disabling exit-node fallback."
  fi

  if [ -n "$CONNECTOR_TAG" ]; then
    validate_connector_tag
    if [ -n "$REGION" ]; then
      local explicit_tag="$CONNECTOR_TAG"
      CONNECTOR_TAG=""
      derive_identity_from_region
      if [ "$CONNECTOR_TAG" != "$explicit_tag" ]; then
        die "CONNECTOR_TAG $explicit_tag conflicts with REGION=$REGION (expected $CONNECTOR_TAG). Set one coherent identity source."
      fi
      CONNECTOR_TAG="$explicit_tag"
    fi
  elif [ -n "$REGION" ]; then
    derive_identity_from_region
    validate_connector_tag
  else
    read_persisted_identity
    if [ "$PERSISTED_IDENTITY_PRESENT" = "1" ]; then
      [ -n "$PERSISTED_CONNECTOR_TAG" ] || die "Persisted connector identity has no CONNECTOR_TAG. Rerun ./bootstrap.sh or delete $GENERATED_DIR/connector-identity.env."
      REGION="$PERSISTED_REGION"
      CONNECTOR_TAG="$PERSISTED_CONNECTOR_TAG"
      validate_connector_tag
    elif [ "$STATUS_CONNECTOR_TAG_COUNT" = "1" ]; then
      CONNECTOR_TAG="$STATUS_CONNECTOR_TAG"
      validate_connector_tag
    fi
  fi

  if [ -n "$CONNECTOR_TAG" ] && [ -n "$STATUS_CONNECTOR_TAG" ] && [ "$CONNECTOR_TAG" != "$STATUS_CONNECTOR_TAG" ]; then
    die "Resolved connector tag $CONNECTOR_TAG conflicts with current Tailscale status tag $STATUS_CONNECTOR_TAG. Refusing to verify the wrong connector."
  fi
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --dry-run)
        DRY_RUN=1
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

json_value() {
  STATUS_JSON_PAYLOAD="$STATUS_JSON" python3 - "$CONNECTOR_TAG" 2>/dev/null <<'PY'
import json
import os
import sys

expected_tag = sys.argv[1]
data = json.loads(os.environ["STATUS_JSON_PAYLOAD"])
self_node = data.get("Self") or {}
tags = self_node.get("Tags") or []
print("1" if expected_tag in tags else "0")
PY
}

preflight() {
  have tailscale || die "Tailscale CLI is not installed."
  have python3 || warn "python3 is unavailable; connector tag verification will be skipped."

  if ! STATUS_JSON="$(tailscale status --json 2>/dev/null)"; then
    die "Tailscale is not running or this host is not logged in."
  fi
  resolve_connector_identity
}

check_connector_after_disable() {
  local tag_present

  have python3 || return 0
  if ! STATUS_JSON="$(tailscale status --json 2>/dev/null)"; then
    warn "Could not verify connector tag after disabling exit-node advertising."
    return 0
  fi
  if ! json_value >/dev/null; then
    warn "Could not parse Tailscale status after disabling exit-node advertising; connector tag verification was skipped."
    return 0
  fi
  resolve_connector_identity

  if [ -z "$CONNECTOR_TAG" ]; then
    warn "Could not identify a connector tag; connector tag verification was skipped."
    return 0
  fi

  if ! tag_present="$(json_value)"; then
    warn "Could not parse Tailscale status after disabling exit-node advertising; connector tag verification was skipped."
    return 0
  fi

  if [ "$tag_present" = "1" ]; then
    note "[OK] Expected connector tag $CONNECTOR_TAG is still present."
  else
    warn "Expected connector tag $CONNECTOR_TAG was not visible after disabling exit-node mode."
    warn "Run ./restore-connector.sh if App Connector routing no longer works."
  fi
}

main() {
  parse_args "$@"
  preflight
  ai_egress_run_root tailscale set --advertise-exit-node=false
  note "[OK] Exit-node advertising disabled."
  if [ "$DRY_RUN" != "1" ]; then
    check_connector_after_disable
  fi
}

main "$@"
