#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

VERSION="$(cat "$ROOT_DIR/VERSION" 2>/dev/null || true)"
VERSION="${VERSION:-1.0.0}"

REGION="${REGION:-}"
REGION_UPPER=""
REGION_LOWER=""
CONNECTOR_NAME="${CONNECTOR_NAME:-}"
CONNECTOR_TAG="${CONNECTOR_TAG:-}"
CONNECTOR_HOSTNAME="${CONNECTOR_HOSTNAME:-}"
TAILSCALE_TAILNET="${TAILSCALE_TAILNET:--}"
TAG_OWNER="${TAG_OWNER:-autogroup:admin}"
MEMBER_SRC="${MEMBER_SRC:-autogroup:member}"
COMMON_DOMAINS_FILE="$ROOT_DIR/policy/default-ai-domains.json"
GENERATED_DIR="${GENERATED_DIR:-$ROOT_DIR/generated}"
DRY_RUN="${DRY_RUN:-0}"
SYSTEM_DEPS_INSTALLED=0

DOMAINS_FILE="$COMMON_DOMAINS_FILE"
CLI_DOMAIN_PACK=""
CLI_DOMAINS_FILE=""
POLICY_APPLIED=0
POLICY_PLAN_DIR=""
AUTH_FILE_TO_CLEAN=""
AUTH_TEMP_DIR_TO_CLEAN=""

usage() {
  cat <<'EOF'
Usage: ./bootstrap.sh [--dry-run] [--domain-pack name] [--domains-file path] [--version] [--help]

Bootstrap a Linux VPS as a Tailscale AI App Connector.

Options:
  --dry-run           Print privileged commands and skip live diagnostics.
  --domain-pack name  Use the common domain pack.
  --domains-file path Use a custom domain file. Conflicts with --domain-pack.
  --version           Print the tailscale-ai-egress version and exit.
  -h, --help          Show this help.

Useful environment variables:
  REGION=us # auto-detected and confirmable in interactive mode when unset
  CONNECTOR_NAME=AI-Egress-US
  CONNECTOR_TAG=tag:ai-egress-us
  CONNECTOR_HOSTNAME=ai-egress-us-01 # or choose a short interactive keyword
  TAG_OWNER=autogroup:admin
  MEMBER_SRC=autogroup:member
  AI_EGRESS_DOMAINS_FILE=/path/to/domains.txt # custom fallback; CLI options win
  GENERATED_DIR=/path/to/output
  TAILSCALE_TAILNET=-
  TAILSCALE_API_KEY=tskey-api-...
  TAILSCALE_OAUTH_CLIENT_ID=...
  TAILSCALE_OAUTH_CLIENT_SECRET=...
  TAILSCALE_AUTHKEY=tskey-auth-... # node auth key, not tskey-api
  POLICY_RISK_ACK=1 # CI/automation only; skips Advanced Mode risk confirmation
  BOOTSTRAP_RESET_ACK=1 # automation only; allows replacing existing Tailscale settings

See README.md for the full environment variable list and examples.
EOF
}

usage_error() {
  printf 'error: %s\n' "$*" >&2
  usage >&2
  exit 2
}

show_version() {
  printf 'tailscale-ai-egress bootstrap.sh %s\n' "$VERSION"
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
      --domain-pack)
        [ -n "${2:-}" ] || usage_error "--domain-pack requires a name."
        CLI_DOMAIN_PACK="$2"
        shift 2
        ;;
      --domains-file)
        [ -n "${2:-}" ] || usage_error "--domains-file requires a path."
        CLI_DOMAINS_FILE="$2"
        shift 2
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

  if [ -n "$CLI_DOMAIN_PACK" ] && [ -n "$CLI_DOMAINS_FILE" ]; then
    usage_error "--domain-pack and --domains-file are mutually exclusive."
  fi

  case "$CLI_DOMAIN_PACK" in
    ""|common) ;;
    *) usage_error "Unknown domain pack '$CLI_DOMAIN_PACK'. Available packs: common." ;;
  esac
}

say() {
  printf '\n%s\n' "$*"
}

note() {
  printf '%s\n' "$*"
}

warn() {
  printf 'warning: %s\n' "$*" >&2
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

cleanup_auth_file() {
  if [ -n "${AUTH_FILE_TO_CLEAN:-}" ]; then
    rm -f "$AUTH_FILE_TO_CLEAN"
    AUTH_FILE_TO_CLEAN=""
  fi
  if [ -n "${AUTH_TEMP_DIR_TO_CLEAN:-}" ]; then
    rm -rf "$AUTH_TEMP_DIR_TO_CLEAN"
    AUTH_TEMP_DIR_TO_CLEAN=""
  fi
}

cleanup() {
  cleanup_auth_file
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

have() {
  command -v "$1" >/dev/null 2>&1
}

is_interactive() {
  [ -t 0 ] || ( : </dev/tty ) 2>/dev/null
}

has_policy_credential() {
  [ -n "${TAILSCALE_API_KEY:-}" ] ||
    { [ -n "${TAILSCALE_OAUTH_CLIENT_ID:-}" ] && [ -n "${TAILSCALE_OAUTH_CLIENT_SECRET:-}" ]; }
}

validate_node_auth_key() {
  local key="$1"
  case "$key" in
    tskey-api-*)
      die "TAILSCALE_AUTHKEY must be a node auth key (tskey-auth-...), not a Tailscale API key (tskey-api-...). Use the API key only for policy updates."
      ;;
    tskey-scim-*|tskey-webhook-*)
      die "TAILSCALE_AUTHKEY has a non-node key prefix. Generate a node auth key from the Tailscale Admin Console Keys page."
      ;;
    tskey-auth-*|tskey-client-*)
      return 0
      ;;
    tskey-*)
      warn "TAILSCALE_AUTHKEY has an uncommon Tailscale key prefix; expected tskey-auth- for a node auth key."
      ;;
  esac
}

validate_config() {
  case "$CONNECTOR_TAG" in
    tag:*) ;;
    *) die "CONNECTOR_TAG must start with 'tag:' (got: $CONNECTOR_TAG)" ;;
  esac

  local tag_name="${CONNECTOR_TAG#tag:}"
  case "$tag_name" in
    ""|*[!a-z0-9-]*|-*|*-)
      die "CONNECTOR_TAG must be tag:<lowercase-alphanumeric-hyphens> (got: $CONNECTOR_TAG)"
      ;;
  esac

  [ -n "$CONNECTOR_NAME" ] || die "CONNECTOR_NAME cannot be empty."
  [ -n "$CONNECTOR_HOSTNAME" ] || die "CONNECTOR_HOSTNAME cannot be empty."
}

read_line() {
  local prompt="$1"
  local answer=""
  if [ -t 0 ]; then
    read -r -p "$prompt" answer
  elif ( : </dev/tty ) 2>/dev/null; then
    if ! read -r -p "$prompt" answer </dev/tty 2>/dev/null; then
      return 1
    fi
  else
    return 1
  fi
  printf '%s' "$answer"
}

read_secret() {
  local prompt="$1"
  local answer=""
  if [ -t 0 ]; then
    read -r -s -p "$prompt" answer
    printf '\n' >&2
  elif ( : </dev/tty ) 2>/dev/null; then
    if ! read -r -s -p "$prompt" answer </dev/tty 2>/dev/null; then
      return 1
    fi
    printf '\n' >/dev/tty 2>/dev/null || true
  else
    return 1
  fi
  printf '%s' "$answer"
}

yes_no() {
  local prompt="$1"
  local default="$2"
  local suffix="[y/N]"
  local answer=""

  if [ "$default" = "Y" ]; then
    suffix="[Y/n]"
  fi

  answer="$(read_line "$prompt $suffix " || true)"
  if [ -z "$answer" ]; then
    answer="$default"
  fi

  case "$answer" in
    y|Y|yes|YES|Yes) return 0 ;;
    *) return 1 ;;
  esac
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

detect_region() {
  local detected=""
  if have curl; then
    detected="$(curl -fsS --connect-timeout 5 --max-time 10 https://ifconfig.co/country-iso 2>/dev/null | tr -d '[:space:]' || true)"
  elif have wget; then
    detected="$(wget -qO- --timeout=10 https://ifconfig.co/country-iso 2>/dev/null | tr -d '[:space:]' || true)"
  fi

  case "$detected" in
    [A-Za-z][A-Za-z])
      normalize_region "$detected"
      ;;
    *)
      printf ''
      ;;
  esac
}

resolve_region() {
  local detected=""
  local default_region=""
  local answer=""
  local normalized=""

  if is_interactive && [ "$DRY_RUN" != "1" ]; then
    detected="$(detect_region)"
    if [ -n "$detected" ]; then
      default_region="$detected"
    fi

    note "Detected region: ${detected:-unknown}"
    while true; do
      if [ -n "$default_region" ]; then
        answer="$(read_line "Region [$default_region]: " || true)"
      else
        answer="$(read_line "Region: " || true)"
      fi
      if [ -z "$answer" ]; then
        answer="$default_region"
      fi

      normalized="$(normalize_region "$answer")"
      if valid_region "$normalized"; then
        REGION="$normalized"
        return 0
      fi

      warn "Region must contain only letters, numbers, and single hyphens, and cannot start or end with a hyphen."
    done
  fi

  if [ "$DRY_RUN" != "1" ]; then
    detected="$(detect_region)"
    if [ -n "$detected" ]; then
      REGION="$detected"
      return 0
    fi
    die "REGION is required when bootstrap cannot auto-detect the VPS country code. Set REGION=us, REGION=sg, REGION=tw, or another short region label."
  fi

  REGION="JP"
}

normalize_hostname_keyword() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9'
}

resolve_hostname() {
  local keyword=""
  local normalized=""
  local length=0

  if ! is_interactive || [ "$DRY_RUN" = "1" ]; then
    CONNECTOR_HOSTNAME="ai-egress-$REGION_LOWER-01"
    return 0
  fi

  while true; do
    keyword="$(read_line "Hostname keyword, 3-5 chars (Enter for 01): " || true)"
    if [ -z "$keyword" ]; then
      CONNECTOR_HOSTNAME="ai-egress-$REGION_LOWER-01"
      return 0
    fi

    normalized="$(normalize_hostname_keyword "$keyword")"
    length="${#normalized}"
    if [ "$length" -ge 3 ] && [ "$length" -le 5 ]; then
      CONNECTOR_HOSTNAME="ai-egress-$REGION_LOWER-$normalized"
      return 0
    fi

    warn "Hostname keyword must normalize to 3-5 letters or numbers; press Enter to use 01."
  done
}

resolve_identity() {
  if [ -z "$REGION" ]; then
    if [ -z "$CONNECTOR_NAME" ] || [ -z "$CONNECTOR_TAG" ] || [ -z "$CONNECTOR_HOSTNAME" ]; then
      resolve_region
    else
      REGION="JP"
    fi
  fi

  REGION_UPPER="$(printf '%s' "$REGION" | tr '[:lower:]' '[:upper:]')"
  REGION_LOWER="$(printf '%s' "$REGION" | tr '[:upper:]' '[:lower:]')"
  if ! valid_region "$REGION_LOWER"; then
    die "REGION must contain only letters, numbers, and single hyphens, and cannot start or end with a hyphen (got: $REGION)"
  fi

  [ -n "$CONNECTOR_NAME" ] || CONNECTOR_NAME="AI-Egress-$REGION_UPPER"
  [ -n "$CONNECTOR_TAG" ] || CONNECTOR_TAG="tag:ai-egress-$REGION_LOWER"
  [ -n "$CONNECTOR_HOSTNAME" ] || resolve_hostname
}

persist_connector_identity() {
  local identity_file="$GENERATED_DIR/connector-identity.env"
  local tmp="$identity_file.tmp"

  mkdir -p "$GENERATED_DIR"
  {
    printf '# Generated by bootstrap.sh after a successful connector setup.\n'
    printf '# Safe to delete; helper scripts will fall back to Tailscale status.\n'
    printf 'REGION=%s\n' "$REGION_LOWER"
    printf 'CONNECTOR_TAG=%s\n' "$CONNECTOR_TAG"
    printf 'CONNECTOR_HOSTNAME=%s\n' "$CONNECTOR_HOSTNAME"
  } >"$tmp"
  chmod 600 "$tmp" 2>/dev/null || true
  mv "$tmp" "$identity_file"
  note "Saved connector identity: $identity_file"
}

run_root() {
  if [ "$DRY_RUN" = "1" ]; then
    printf '+'
    if [ "$(id -u)" -ne 0 ]; then
      printf ' sudo'
    fi
    printf ' %q' "$@"
    printf '\n'
    return 0
  fi

  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

domain_pack_file() {
  case "$1" in
    common)
      printf '%s\n' "$COMMON_DOMAINS_FILE"
      ;;
    *)
      die "Unknown domain pack '$1'. Available packs: common."
      ;;
  esac
}

domain_pack_label_for_file() {
  case "$1" in
    "$COMMON_DOMAINS_FILE") printf 'common\n' ;;
    *) printf 'custom\n' ;;
  esac
}

validate_domains_file() {
  python3 "$ROOT_DIR/scripts/policy_tool.py" domains --domains-file "$DOMAINS_FILE" >/dev/null
}

check_required_deps() {
  local missing=""
  local cmd
  for cmd in python3 install sysctl; do
    if ! have "$cmd"; then
      missing="$missing $cmd"
    fi
  done

  if ! have tailscale && ! have curl; then
    missing="$missing curl"
  fi

  if [ -n "$missing" ]; then
    die "Missing required command(s):$missing. Install them manually, then re-run bootstrap."
  fi
}

install_system_deps() {
  say "Installing system dependencies"
  if have apt-get; then
    run_root apt-get update
    run_root env DEBIAN_FRONTEND=noninteractive apt-get install -y \
      ca-certificates curl jq python3 dnsutils traceroute whois iproute2 ethtool
  elif have dnf; then
    run_root dnf install -y ca-certificates curl jq python3 bind-utils traceroute whois iproute ethtool
  elif have yum; then
    run_root yum install -y ca-certificates curl jq python3 bind-utils traceroute whois iproute ethtool
  elif have apk; then
    run_root apk add --no-cache ca-certificates curl jq python3 bind-tools traceroute whois iproute2 ethtool
  else
    warn "No supported package manager detected. Please install curl, python3, dig, traceroute, whois, iproute2, and ethtool manually."
  fi
}

maybe_install_system_deps_before_identity() {
  if [ "$DRY_RUN" = "1" ]; then
    return 0
  fi
  if [ -n "$REGION" ]; then
    return 0
  fi
  if [ -n "$CONNECTOR_NAME" ] && [ -n "$CONNECTOR_TAG" ] && [ -n "$CONNECTOR_HOSTNAME" ]; then
    return 0
  fi

  install_system_deps
  SYSTEM_DEPS_INSTALLED=1
}

install_tailscale_if_needed() {
  if have tailscale; then
    note "Tailscale is already installed."
    return 0
  fi

  say "Installing Tailscale"
  # Tailscale documents https://tailscale.com/install.sh as its Linux installer.
  # It handles privilege escalation internally when the current user is non-root.
  if [ "$DRY_RUN" = "1" ]; then
    note "+ curl -fsSL https://tailscale.com/install.sh | sh"
    return 0
  fi
  curl -fsSL https://tailscale.com/install.sh | sh
}

enable_ip_forwarding() {
  say "Enabling IP forwarding"
  local tmp
  local ipv6_available=0
  tmp="$(mktemp)"
  printf 'net.ipv4.ip_forward = 1\n' >"$tmp"
  if sysctl net.ipv6.conf.all.forwarding >/dev/null 2>&1; then
    printf 'net.ipv6.conf.all.forwarding = 1\n' >>"$tmp"
    ipv6_available=1
  fi

  run_root install -m 0644 "$tmp" /etc/sysctl.d/99-tailscale-ai-egress.conf
  rm -f "$tmp"

  run_root sysctl -w net.ipv4.ip_forward=1
  if [ "$ipv6_available" = "1" ]; then
    run_root sysctl -w net.ipv6.conf.all.forwarding=1
  else
    warn "IPv6 forwarding sysctl is unavailable on this host."
  fi
}

show_domains_file() {
  python3 "$ROOT_DIR/scripts/policy_tool.py" domains --domains-file "$1"
}

select_domain_pack_interactive() {
  note "Which domains?"
  note "  common - ChatGPT, Claude, Poe, OpenRouter, Perplexity (default)"
  note "  custom - enter domains manually"

  local answer
  answer="$(read_line "Domains [common]: " || true)"
  case "$answer" in
    ""|common|Common|COMMON)
      DOMAINS_FILE="$COMMON_DOMAINS_FILE"
      ;;
    custom|Custom|CUSTOM)
      collect_custom_domains
      ;;
    *)
      warn "Unknown selection '$answer'; using common."
      DOMAINS_FILE="$COMMON_DOMAINS_FILE"
      ;;
  esac
}

collect_custom_domains() {
  mkdir -p "$GENERATED_DIR"
  local custom_file="$GENERATED_DIR/custom-ai-domains.txt"

  if [ -s "$custom_file" ]; then
    note "Found previous custom domain list:"
    note "$custom_file"
    if yes_no "Reuse this custom domain list?" "Y"; then
      DOMAINS_FILE="$custom_file"
      validate_domains_file
      return 0
    fi
  fi

  local tmp_custom="$GENERATED_DIR/custom-ai-domains.tmp"
  : >"$tmp_custom"

  note "Enter domains one per line. Use a blank line to finish."

  while true; do
    local domain
    domain="$(read_line "domain> " || true)"
    if [ -z "$domain" ]; then
      break
    fi
    printf '%s\n' "$domain" >>"$tmp_custom"
  done

  if [ ! -s "$tmp_custom" ]; then
    rm -f "$tmp_custom"
    warn "No custom domains entered; falling back to the common domain pack."
    DOMAINS_FILE="$COMMON_DOMAINS_FILE"
  else
    DOMAINS_FILE="$tmp_custom"
    validate_domains_file
    mv "$tmp_custom" "$custom_file"
    DOMAINS_FILE="$custom_file"
  fi
}

collect_domains() {
  say "Choosing AI domains"
  mkdir -p "$GENERATED_DIR"

  if [ -n "$CLI_DOMAIN_PACK" ]; then
    DOMAINS_FILE="$(domain_pack_file "$CLI_DOMAIN_PACK")"
    validate_domains_file
    note "Domain pack: $CLI_DOMAIN_PACK"
  elif [ -n "$CLI_DOMAINS_FILE" ]; then
    DOMAINS_FILE="$CLI_DOMAINS_FILE"
    validate_domains_file
    note "Domain file: $DOMAINS_FILE"
  elif [ -n "${AI_EGRESS_DOMAINS_FILE:-}" ]; then
    DOMAINS_FILE="$AI_EGRESS_DOMAINS_FILE"
    if validate_domains_file; then
      note "Domain file from AI_EGRESS_DOMAINS_FILE: $DOMAINS_FILE"
    else
      warn "Could not validate domain file from AI_EGRESS_DOMAINS_FILE: $DOMAINS_FILE; falling back to the common domain pack."
      DOMAINS_FILE="$COMMON_DOMAINS_FILE"
      validate_domains_file
      note "Domain pack: common"
    fi
  elif is_interactive; then
    select_domain_pack_interactive
    validate_domains_file
    note "Domain selection: $(domain_pack_label_for_file "$DOMAINS_FILE")"
  else
    DOMAINS_FILE="$COMMON_DOMAINS_FILE"
    validate_domains_file
    note "Domain pack: common"
  fi

  note "Selected domains:"
  show_domains_file "$DOMAINS_FILE" | sed 's/^/  - /'
}

confirm_policy_risk() {
  say "Policy safety review"
  warn "Advanced Mode will add a broad grant: $MEMBER_SRC -> autogroup:internet on all ports."
  warn "Advanced Mode will auto-approve 0.0.0.0/0 and ::/0 routes for $CONNECTOR_TAG."
  warn "Only continue if this matches your tailnet security model."

  if [ "${POLICY_RISK_ACK:-0}" = "1" ]; then
    note "POLICY_RISK_ACK=1 set; skipping interactive policy risk confirmation. This is intended for CI/automation only."
    return 0
  fi

  if ! yes_no "Proceed with applying these policy changes?" "N"; then
    warn "Automatic policy apply cancelled; continuing with guided manual setup."
    return 1
  fi
}

maybe_apply_policy() {
  say "Tailscale policy setup"
  note "Advanced Mode can update your Tailscale policy automatically."
  note "Most users should paste the generated snippet manually."
  if ! yes_no "Do you want this installer to update your Tailscale policy automatically? This is Advanced Mode." "N"; then
    return 0
  fi

  if ! has_policy_credential; then
    local token
    token="$(read_secret "Tailscale API token (leave blank for guided manual mode): " || true)"
    if [ -n "$token" ]; then
      export TAILSCALE_API_KEY="$token"
    fi
  fi

  if ! has_policy_credential; then
    warn "No API/OAuth credential provided; continuing with guided manual policy setup."
    return 0
  fi

  if ! confirm_policy_risk; then
    return 0
  fi

  local plan_output=""
  if ! plan_output="$(python3 "$ROOT_DIR/scripts/policy_tool.py" plan \
    --domains-file "$DOMAINS_FILE" \
    --connector-name "$CONNECTOR_NAME" \
    --connector-tag "$CONNECTOR_TAG" \
    --tag-owner "$TAG_OWNER" \
    --member-src "$MEMBER_SRC" \
    --tailnet "$TAILSCALE_TAILNET" \
    --plans-dir "$GENERATED_DIR/policy-plans" 2>&1)"; then
    printf '%s\n' "$plan_output"
    warn "Automatic policy planning failed; falling back to guided manual setup."
    return 0
  fi

  printf '%s\n' "$plan_output"

  local plan_dir plan_id expected answer
  plan_dir="$(printf '%s\n' "$plan_output" | sed -n 's/^Plan directory: //p' | tail -n 1)"
  plan_id="$(printf '%s\n' "$plan_output" | sed -n 's/^Plan ID: //p' | tail -n 1)"
  if [ -z "$plan_dir" ] || [ -z "$plan_id" ]; then
    warn "Could not identify the generated plan; falling back to guided manual setup."
    return 0
  fi

  note "Review before applying:"
  note "  $plan_dir/diff.patch"
  note "  $plan_dir/manifest.json"

  expected="APPLY $plan_id"
  answer="$(read_line "Type \"$expected\" to apply this exact plan, or press Enter for guided manual setup: " || true)"
  if [ "$answer" != "$expected" ]; then
    warn "Plan was not applied; continuing with guided manual setup."
    return 0
  fi

  if POLICY_RISK_ACK=1 python3 "$ROOT_DIR/scripts/policy_tool.py" apply-plan \
    --tailnet "$TAILSCALE_TAILNET" \
    --yes \
    "$plan_dir"; then
    POLICY_APPLIED=1
    POLICY_PLAN_DIR="$plan_dir"
  else
    warn "Automatic policy apply-plan failed; falling back to guided manual setup."
  fi
}

manual_policy_instructions() {
  if [ "$POLICY_APPLIED" = "1" ]; then
    return 0
  fi

  say "Manual Tailscale policy setup"
  note "Open this page:"
  note "https://login.tailscale.com/admin/acls/file"

  local snippet="$GENERATED_DIR/app-connector.snippet.json"
  python3 "$ROOT_DIR/scripts/policy_tool.py" snippet \
    --domains-file "$DOMAINS_FILE" \
    --connector-name "$CONNECTOR_NAME" \
    --connector-tag "$CONNECTOR_TAG" \
    --tag-owner "$TAG_OWNER" \
    --member-src "$MEMBER_SRC" >"$snippet"

  note "Merge this generated snippet into your tailnet policy:"
  note "$snippet"
  note "Treat files under $GENERATED_DIR as sensitive; they can contain tailnet policy details."

  note "Save the policy, then come back here."
  read_line "Press Enter to continue after saving the policy..." >/dev/null || true
}

tailscale_up_connector() {
  say "Configuring this VPS as a Tailscale app connector"
  local self_status=""
  local needs_reset=0
  if have tailscale; then
    if ! self_status="$(tailscale status --self 2>/dev/null)"; then
      self_status=""
    elif printf '%s
' "$self_status" | grep -qi 'tailscale cli failed'; then
      self_status=""
    fi
  fi

  if [ -n "$self_status" ]; then
    if printf '%s
' "$self_status" | grep -qF "$CONNECTOR_HOSTNAME"; then
      if [ "${BOOTSTRAP_RESET_ACK:-0}" = "1" ]; then
        note "BOOTSTRAP_RESET_ACK=1 set; re-running tailscale up --reset for $CONNECTOR_HOSTNAME."
        needs_reset=1
      elif ! is_interactive; then
        return 0
      elif ! yes_no "Tailscale is already running as $CONNECTOR_HOSTNAME. Re-run tailscale up --reset anyway?" "N"; then
        return 0
      else
        needs_reset=1
      fi
    else
      warn "Tailscale already appears to be configured differently on this host:"
      note "$self_status"
      if [ "${BOOTSTRAP_RESET_ACK:-0}" = "1" ]; then
        note "BOOTSTRAP_RESET_ACK=1 set; replacing existing local Tailscale settings."
        needs_reset=1
      elif ! is_interactive; then
        die "Tailscale is already configured differently on this host. Set BOOTSTRAP_RESET_ACK=1 to allow tailscale up --reset in non-interactive mode."
      elif ! yes_no "Continue and run tailscale up --reset, replacing the existing local Tailscale settings?" "N"; then
        return 0
      else
        needs_reset=1
      fi
    fi
  fi

  local auth_key="${TAILSCALE_AUTHKEY:-${TS_AUTH_KEY:-}}"
  if [ -z "$auth_key" ]; then
    if is_interactive; then
      auth_key="$(read_secret "Tailscale node auth key, tskey-auth-* (leave blank for browser login): " || true)"
    elif [ "$DRY_RUN" != "1" ]; then
      die "TAILSCALE_AUTHKEY is required in non-interactive mode; otherwise tailscale up waits for browser login."
    fi
  fi

  local auth_file=""
  local auth_dir=""
  local -a up_cmd=(
    tailscale up
    --hostname="$CONNECTOR_HOSTNAME"
    --advertise-connector
    --advertise-tags="$CONNECTOR_TAG"
  )
  if [ "$needs_reset" = "1" ]; then
    up_cmd=(tailscale up --reset "${up_cmd[@]:2}")
  fi
  if [ -n "$auth_key" ]; then
    validate_node_auth_key "$auth_key"
    auth_dir="$(mktemp -d)"
    chmod 700 "$auth_dir"
    auth_file="$auth_dir/authkey"
    : >"$auth_file"
    chmod 600 "$auth_file"
    printf '%s' "$auth_key" >"$auth_file"
    AUTH_FILE_TO_CLEAN="$auth_file"
    AUTH_TEMP_DIR_TO_CLEAN="$auth_dir"
    up_cmd+=(--auth-key="file:$auth_file")
  fi

  if ! run_root "${up_cmd[@]}"; then
    cleanup_auth_file
    return 1
  fi

  cleanup_auth_file
}

print_policy_rollback_hint() {
  if [ "$POLICY_APPLIED" != "1" ]; then
    return 0
  fi

  warn "Policy was applied, but the connector failed to start."
  warn "Policy plans are in: $GENERATED_DIR/policy-plans"
  warn "To restore an applied plan, run: python3 scripts/policy_tool.py list-plans --plans-dir \"$GENERATED_DIR/policy-plans\""
  warn "Then run: python3 scripts/policy_tool.py restore-plan <plan-dir>"
}

restore_applied_policy_after_connector_failure() {
  if [ "$POLICY_APPLIED" != "1" ]; then
    return 0
  fi

  warn "Policy was applied, but the connector failed to start."
  if [ -z "$POLICY_PLAN_DIR" ]; then
    print_policy_rollback_hint
    return 1
  fi

  warn "Attempting to restore the pre-apply tailnet policy from: $POLICY_PLAN_DIR"
  if POLICY_RISK_ACK=1 python3 "$ROOT_DIR/scripts/policy_tool.py" restore-plan \
    --tailnet "$TAILSCALE_TAILNET" \
    --yes \
    "$POLICY_PLAN_DIR"; then
    warn "Restored the pre-apply tailnet policy after connector startup failed."
    return 0
  fi

  warn "Automatic policy restore failed."
  print_policy_rollback_hint
  return 1
}

run_diagnostics() {
  say "Running diagnostics"
  if [ "$DRY_RUN" = "1" ]; then
    note "Dry run enabled; skipping live diagnostics."
    return 0
  fi
  "$ROOT_DIR/diagnose.sh" --domains-file "$DOMAINS_FILE" || warn "Diagnostics reported a problem. See the output above."
}

main() {
  parse_args "$@"
  maybe_install_system_deps_before_identity
  resolve_identity
  validate_config

  local mode_suffix=""
  if [ "$DRY_RUN" = "1" ]; then
    mode_suffix=" (dry-run)"
  fi

  say "Tailscale AI Egress Connector Bootstrap$mode_suffix"
  note "Connector name:     $CONNECTOR_NAME"
  note "Connector tag:      $CONNECTOR_TAG"
  note "Connector hostname: $CONNECTOR_HOSTNAME"

  if [ "$SYSTEM_DEPS_INSTALLED" != "1" ]; then
    install_system_deps
  fi
  check_required_deps
  install_tailscale_if_needed
  enable_ip_forwarding
  collect_domains
  maybe_apply_policy
  manual_policy_instructions
  if ! tailscale_up_connector; then
    restore_applied_policy_after_connector_failure
    exit 1
  fi
  if [ "$DRY_RUN" != "1" ]; then
    persist_connector_identity
  fi
  run_diagnostics

  say "Done"
  note "If diagnostics show AI domains routing through Tailscale, the connector is ready."
  note "App Connector DNS discovery and route advertisement can take 1-2 minutes."
  note "On your client device, clone this repo or download check-client-routes.sh, then run:"
  note "  ./check-client-routes.sh"
  note "If routes are not visible yet, wait and run the client check again."
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
