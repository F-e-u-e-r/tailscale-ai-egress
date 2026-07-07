#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="$(cat "$SCRIPT_DIR/VERSION" 2>/dev/null || true)"
VERSION="${VERSION:-1.1.1}"

REGION="${REGION:-}"
REGION_LOWER=""
GENERATED_DIR="${GENERATED_DIR:-$SCRIPT_DIR/generated}"
CONNECTOR_TAG="${CONNECTOR_TAG:-}"
REQUESTED_REGION="$REGION"
REQUESTED_CONNECTOR_TAG="$CONNECTOR_TAG"
DRY_RUN="${DRY_RUN:-0}"
USE_SUDO="${AI_EGRESS_USE_SUDO:-1}"
ACK="${EXIT_NODE_ACK:-0}"
STATUS_JSON=""
PERSISTED_IDENTITY_PRESENT=0
PERSISTED_REGION=""
PERSISTED_CONNECTOR_TAG=""
STATUS_CONNECTOR_TAG_COUNT=0
STATUS_CONNECTOR_TAG=""

usage() {
  cat <<'EOF'
Usage: ./enable-exit-node.sh [--dry-run] [--yes]

Advertise this already-bootstrapped Linux App Connector as a Tailscale exit
node fallback. This is a full-traffic mode and can use much more VPS transfer
than App Connector mode.

Options:
  --dry-run  Print privileged commands without changing the host.
  --yes      Confirm the VPS transfer warning non-interactively.
  --version  Print the tailscale-ai-egress version and exit.
  -h, --help Show this help.

Useful environment variables:
  REGION=us
  CONNECTOR_TAG=tag:ai-egress-us
  GENERATED_DIR=/path/to/output
  EXIT_NODE_ACK=1
EOF
}

usage_error() {
  printf 'error: %s\n' "$*" >&2
  usage >&2
  exit 2
}

show_version() {
  printf 'tailscale-ai-egress enable-exit-node.sh %s\n' "$VERSION"
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
    die "Could not parse Tailscale status JSON. Refusing to enable exit-node fallback."
  fi
  if [ "$STATUS_CONNECTOR_TAG_COUNT" -gt 1 ]; then
    die "Tailscale status contains multiple tag:ai-egress-* tags on this node. Remove the ambiguous tags before enabling exit-node fallback."
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
    die "Resolved connector tag $CONNECTOR_TAG conflicts with current Tailscale status tag $STATUS_CONNECTOR_TAG. Refusing to enable fallback on the wrong connector."
  fi
}

run_root() {
  if [ "$DRY_RUN" = "1" ]; then
    printf '+'
    if [ "$(id -u)" -ne 0 ] && [ "$USE_SUDO" != "0" ]; then
      printf ' sudo'
    fi
    printf ' %q' "$@"
    printf '\n'
    return 0
  fi

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
      --yes)
        ACK=1
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
  local mode="$1"
  STATUS_JSON_PAYLOAD="$STATUS_JSON" python3 - "$mode" "$CONNECTOR_TAG" 2>/dev/null <<'PY'
import json
import os
import sys

mode = sys.argv[1]
expected_tag = sys.argv[2]
data = json.loads(os.environ["STATUS_JSON_PAYLOAD"])
self_node = data.get("Self") or {}

if mode == "online":
    print("1" if self_node.get("Online", True) else "0")
elif mode == "has-tag":
    tags = self_node.get("Tags") or []
    print("1" if expected_tag in tags else "0")
elif mode == "exit-node":
    allowed_ips = self_node.get("AllowedIPs") or []
    recursive_values = []

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if "ExitNode" in str(key) or "AdvertiseExitNode" in str(key):
                    recursive_values.append(item)
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(self_node)
    advertised = "0.0.0.0/0" in allowed_ips or "::/0" in allowed_ips or any(item is True for item in recursive_values)
    print("1" if advertised else "0")
PY
}

preflight() {
  local has_tag online

  [ "$(uname -s)" = "Linux" ] || die "Exit-node fallback helper is Linux-only. Use docs/Home-Mac-Exit-Node.md for Mac fallback."
  have python3 || die "python3 is required to inspect Tailscale status."
  have sysctl || die "sysctl is required to enable IP forwarding."
  have install || die "install is required to write the forwarding sysctl file."
  have tailscale || die "Tailscale CLI is not installed. Run ./bootstrap.sh first on the connector host."

  if ! STATUS_JSON="$(tailscale status --json 2>/dev/null)"; then
    die "Tailscale is not running or this host is not logged in. Run ./bootstrap.sh first."
  fi
  resolve_connector_identity
  [ -n "$CONNECTOR_TAG" ] || die "Could not identify a connector tag. Set CONNECTOR_TAG or REGION, rerun ./bootstrap.sh to create generated/connector-identity.env, or make sure this node has a tag:ai-egress-* tag."

  if ! online="$(json_value online)"; then
    die "Could not parse Tailscale status JSON. Refusing to enable exit-node fallback."
  fi
  if [ "$online" != "1" ]; then
    die "Tailscale reports this host is offline. Start tailscaled and rerun this helper."
  fi

  if ! has_tag="$(json_value has-tag)"; then
    die "Could not parse Tailscale status JSON. Refusing to enable exit-node fallback."
  fi
  if [ "$has_tag" != "1" ]; then
    die "Expected connector tag $CONNECTOR_TAG was not found. This helper only switches an already-bootstrapped connector; run ./bootstrap.sh first."
  fi
}

confirm_transfer_risk() {
  warn "Exit-node fallback routes all selected-client internet traffic through this VPS."
  warn "Cloud VPS exit-node mode can consume far more data transfer than App Connector mode."
  warn "Use this as an emergency fallback unless you have checked your provider's bandwidth and cost model."

  if [ "$ACK" = "1" ]; then
    note "[OK] EXIT_NODE_ACK/--yes set; continuing with explicit full-traffic fallback mode."
    return 0
  fi

  yes_no "Enable full-traffic exit-node fallback on this VPS?" || die "Cancelled; exit-node fallback was not enabled."
}

enable_forwarding() {
  local tmp
  local ipv6_available=0
  tmp="$(mktemp)"

  printf 'net.ipv4.ip_forward = 1\n' >"$tmp"
  if sysctl net.ipv6.conf.all.forwarding >/dev/null 2>&1; then
    printf 'net.ipv6.conf.all.forwarding = 1\n' >>"$tmp"
    ipv6_available=1
  fi

  run_root install -m 0644 "$tmp" /etc/sysctl.d/99-tailscale-ai-egress.conf
  run_root sysctl -w net.ipv4.ip_forward=1
  if [ "$ipv6_available" = "1" ]; then
    run_root sysctl -w net.ipv6.conf.all.forwarding=1
  else
    warn "IPv6 forwarding sysctl is unavailable on this host."
  fi
  rm -f "$tmp"
}

verify_exit_node_after_enable() {
  local advertised

  have python3 || {
    warn "python3 is unavailable; exit-node verification skipped."
    return 0
  }
  if ! STATUS_JSON="$(tailscale status --json 2>/dev/null)"; then
    warn "Could not verify Tailscale status after enabling exit-node fallback."
    return 0
  fi

  if ! advertised="$(json_value exit-node)"; then
    warn "Could not parse Tailscale status after enabling exit-node fallback; verification was skipped."
    return 0
  fi

  if [ "$advertised" = "1" ]; then
    note "[OK] Exit-node advertising is visible in Tailscale status."
  else
    warn "Exit-node advertising was not visible after enabling fallback."
    warn "Check Tailscale status and Admin Console route approval before relying on this node."
  fi
}

main() {
  parse_args "$@"
  preflight
  confirm_transfer_risk
  enable_forwarding
  run_root tailscale set --advertise-exit-node
  note "[OK] Exit-node fallback is advertised. Approve/use it from Tailscale clients or Admin Console if required."
  if [ "$DRY_RUN" != "1" ]; then
    verify_exit_node_after_enable
  fi
}

main "$@"
