#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="$(cat "$ROOT_DIR/VERSION" 2>/dev/null || true)"
VERSION="${VERSION:-1.0.0}"
DOMAIN_PACK="common"
DOMAINS_FILE="$ROOT_DIR/policy/default-ai-domains.json"
WARN_COUNT=0
FAIL_COUNT=0
JSON_OUTPUT=0
JSON_EVENTS_FILE=""
STATUS_JSON=""
STATUS_TEXT=""
tmp_files=()

usage() {
  cat <<'EOF'
Usage: ./diagnose.sh [--domain-pack name] [--domains-file path] [--json] [--version]

VPS-side diagnostics for a Tailscale AI App Connector host. Run
check-client-routes.sh on a client device to verify client routing.
Connector detection currently follows the tag:ai-egress-* convention.

Options:
  --domain-pack name  Use the common domain pack.
  --domains-file path Use a custom domain file. Conflicts with --domain-pack.
  --json              Emit schema_version=1 JSON instead of text.
  --version           Print the tailscale-ai-egress version and exit.
  -h, --help          Show this help.
EOF
}

usage_error() {
  printf 'error: %s\n' "$*" >&2
  usage >&2
  exit 2
}

show_version() {
  printf 'tailscale-ai-egress diagnose.sh %s\n' "$VERSION"
}

# shellcheck disable=SC2317,SC2329 # Invoked by EXIT/INT/TERM traps.
cleanup() {
  local file
  if [ "${#tmp_files[@]}" -eq 0 ]; then
    return 0
  fi
  for file in "${tmp_files[@]}"; do
    rm -f "$file"
  done
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

have() {
  command -v "$1" >/dev/null 2>&1
}

record() {
  local status="$1"
  local message="$2"
  local check_id="${3:-general}"
  local details="${4:-}"
  local json_status
  if [ -z "$details" ]; then
    details="{}"
  fi
  case "$status" in
    OK) json_status="ok" ;;
    WARN) json_status="warn" ;;
    FAIL) json_status="fail" ;;
    *) json_status="info" ;;
  esac

  if [ "$JSON_OUTPUT" = "1" ]; then
    printf '%s\t%s\t%s\t%s\n' "$check_id" "$json_status" "$message" "$details" >>"$JSON_EVENTS_FILE"
  else
    printf '[%s] %s\n' "$status" "$message"
  fi

  case "$status" in
    WARN) WARN_COUNT=$((WARN_COUNT + 1)) ;;
    FAIL) FAIL_COUNT=$((FAIL_COUNT + 1)) ;;
  esac
}

section() {
  if [ "$JSON_OUTPUT" != "1" ]; then
    printf '\n== %s ==\n' "$1"
  fi
}

emit_json() {
  python3 - "$JSON_EVENTS_FILE" "$WARN_COUNT" "$FAIL_COUNT" <<'PY'
import json
import sys
from pathlib import Path

events_file = Path(sys.argv[1])
warn_count = int(sys.argv[2])
fail_count = int(sys.argv[3])
checks = []
if events_file.exists():
    for line in events_file.read_text(encoding="utf-8").splitlines():
        check_id, status, message, details_raw = line.split("\t", 3)
        try:
            details = json.loads(details_raw)
        except json.JSONDecodeError:
            details = {"raw": details_raw}
        checks.append({
            "id": check_id,
            "status": status,
            "message": message,
            "details": details,
        })

json.dump({
    "schema_version": 1,
    "script": "diagnose.sh",
    "summary": {
        "ok": sum(1 for check in checks if check["status"] == "ok"),
        "warn": warn_count,
        "fail": fail_count,
    },
    "checks": checks,
}, sys.stdout, indent=2)
sys.stdout.write("\n")
PY
}

finish() {
  local exit_code=0
  if [ "$FAIL_COUNT" -gt 0 ]; then
    exit_code=1
  fi

  if [ "$JSON_OUTPUT" = "1" ]; then
    emit_json
    exit "$exit_code"
  fi

  if [ "$exit_code" = "1" ]; then
    printf 'Diagnostics finished with %s failure(s) and %s warning(s).\n' "$FAIL_COUNT" "$WARN_COUNT" >&2
    exit "$exit_code"
  elif [ "$WARN_COUNT" -gt 0 ]; then
    printf 'Diagnostics finished with %s warning(s).\n' "$WARN_COUNT" >&2
  fi
  exit 0
}

domain_pack_file() {
  case "$1" in
    common) printf '%s\n' "$ROOT_DIR/policy/default-ai-domains.json" ;;
    *) usage_error "Unknown domain pack '$1'. Available packs: common." ;;
  esac
}

parse_args() {
  local domain_pack_set=0
  local domains_file_set=0
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --domain-pack)
        [ -n "${2:-}" ] || usage_error "--domain-pack requires a name."
        DOMAIN_PACK="$2"
        domain_pack_set=1
        shift 2
        ;;
      --domains-file)
        [ -n "${2:-}" ] || usage_error "--domains-file requires a path."
        DOMAINS_FILE="$2"
        domains_file_set=1
        shift 2
        ;;
      --json)
        JSON_OUTPUT=1
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

  if [ "$domain_pack_set" = "1" ] && [ "$domains_file_set" = "1" ]; then
    usage_error "--domain-pack and --domains-file are mutually exclusive."
  fi

  if [ "$domain_pack_set" = "1" ]; then
    case "$DOMAIN_PACK" in
      common) ;;
      *) usage_error "Unknown domain pack '$DOMAIN_PACK'. Available packs: common." ;;
    esac
    DOMAINS_FILE="$(domain_pack_file "$DOMAIN_PACK")"
  fi
}

curl_get() {
  local url="$1"
  curl -fsS --connect-timeout 5 --max-time 10 "$url" 2>/dev/null
}

sysctl_value() {
  local key="$1"
  sysctl -n "$key" 2>/dev/null || true
}

resolve_ipv4_all() {
  local domain="$1"
  if have dig; then
    dig +short "$domain" A | awk '/^[0-9.]+$/ { print }'
  elif have getent; then
    getent ahostsv4 "$domain" | awk '{ print $1 }' | awk '!seen[$0]++'
  else
    return 1
  fi
}

route_get() {
  local ip_addr="$1"
  case "$(uname -s)" in
    Darwin)
      if have route; then
        route -n get "$ip_addr" 2>/dev/null || true
      fi
      ;;
    Linux)
      if have ip; then
        ip route get "$ip_addr" 2>/dev/null || true
      fi
      ;;
    *)
      return 1
      ;;
  esac
}

parse_route_interface() {
  case "$(uname -s)" in
    Darwin)
      awk '/interface:/{ print $2; exit }'
      ;;
    Linux)
      awk '{ for (i = 1; i <= NF; i++) if ($i == "dev") { print $(i + 1); exit } }'
      ;;
  esac
}

tailscale_json_value() {
  local mode="$1"
  STATUS_JSON_PAYLOAD="$STATUS_JSON" python3 - "$mode" <<'PY'
import json
import os
import sys

mode = sys.argv[1]
data = json.loads(os.environ["STATUS_JSON_PAYLOAD"])
self_node = data.get("Self") or {}

if mode == "connector-tag":
    tags = self_node.get("Tags") or []
    # Project diagnostics currently treat tag:ai-egress-* as the connector convention.
    # Custom connector tags can still work, but they are not detected here yet.
    print("1" if any(str(tag).startswith("tag:ai-egress-") for tag in tags) else "0")
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

check_public_egress() {
  section "Public egress"
  if ! have curl; then
    record FAIL "curl is not installed." "public-egress"
    return 0
  fi

  local ipv4
  local ipv6
  local asn
  ipv4="$(curl_get https://ifconfig.co/ip || true)"
  ipv6="$(curl -6 -fsS --connect-timeout 5 --max-time 10 https://ifconfig.co/ip 2>/dev/null || true)"
  asn="$(curl_get https://ifconfig.co/asn || true)"

  if [ -n "$ipv4" ]; then
    record OK "IPv4 egress: $ipv4" "public-egress"
  else
    record FAIL "IPv4 egress unavailable." "public-egress"
  fi

  if [ -n "$ipv6" ]; then
    record OK "IPv6 egress: $ipv6" "public-egress"
  else
    record WARN "IPv6 egress unavailable or unsupported by this VPS." "public-egress"
  fi

  if [ -n "$asn" ]; then
    record OK "ASN: $asn" "public-egress"
  else
    record WARN "ASN lookup unavailable." "public-egress"
  fi
}

check_forwarding() {
  section "Forwarding"
  if ! have sysctl; then
    record FAIL "sysctl is not available." "forwarding"
    return 0
  fi

  local ipv4_forward
  local ipv6_forward
  ipv4_forward="$(sysctl_value net.ipv4.ip_forward)"
  ipv6_forward="$(sysctl_value net.ipv6.conf.all.forwarding)"

  if [ "$ipv4_forward" = "1" ]; then
    record OK "Forwarding enabled: net.ipv4.ip_forward=1" "forwarding" '{"family":"ipv4","enabled":true}'
  else
    record FAIL "Forwarding enabled: no; net.ipv4.ip_forward is not enabled." "forwarding" '{"family":"ipv4","enabled":false}'
  fi

  if [ "$ipv6_forward" = "1" ]; then
    record OK "Forwarding enabled: net.ipv6.conf.all.forwarding=1" "forwarding" '{"family":"ipv6","enabled":true}'
  elif [ -n "$ipv6_forward" ]; then
    record WARN "Forwarding enabled: IPv6 forwarding is not enabled." "forwarding" '{"family":"ipv6","enabled":false}'
  else
    record WARN "Forwarding enabled: IPv6 forwarding sysctl unavailable." "forwarding" '{"family":"ipv6","enabled":null}'
  fi
}

check_tailscale() {
  section "Tailscale"
  if ! have tailscale; then
    record FAIL "tailscale is not installed." "tailscale-status"
    return 0
  fi

  local version
  version="$(tailscale version 2>/dev/null | head -n 1 || true)"
  [ -n "$version" ] && record OK "Tailscale version: $version" "tailscale-version"

  STATUS_JSON="$(tailscale status --json 2>/dev/null || true)"
  STATUS_TEXT="$(tailscale status --self 2>/dev/null || tailscale status 2>/dev/null || true)"
  if [ -n "$STATUS_TEXT" ] && ! printf '%s\n' "$STATUS_TEXT" | grep -qi 'tailscale cli failed'; then
    record OK "Tailscale status is available." "tailscale-status"
    if [ "$JSON_OUTPUT" != "1" ]; then
      printf '%s\n' "$STATUS_TEXT" | sed 's/^/  /'
    fi
  else
    record FAIL "Tailscale is not running or not logged in." "tailscale-status"
  fi
}

check_tailscale_modes() {
  section "Tailscale mode"
  if ! have tailscale; then
    return 0
  fi

  if [ -z "$STATUS_JSON" ] && [ -z "$STATUS_TEXT" ]; then
    record WARN "Connector advertised: unknown; Tailscale status was unavailable." "connector-advertised" '{"advertised":null}'
    record WARN "Exit node advertised: unknown; Tailscale status was unavailable." "exit-node-advertised" '{"advertised":null}'
    return 0
  fi

  if [ -n "$STATUS_JSON" ] && have python3 && [ "$(tailscale_json_value connector-tag 2>/dev/null || printf '0')" = "1" ]; then
    record OK "Connector advertised: expected ai-egress tag is present." "connector-advertised" '{"advertised":true}'
  elif printf '%s\n' "$STATUS_TEXT" | grep -q 'tag:ai-egress-'; then
    record OK "Connector advertised: expected ai-egress tag is present." "connector-advertised" '{"advertised":true}'
  else
    record WARN "Connector advertised: unknown; expected ai-egress tag was not visible in Tailscale status." "connector-advertised" '{"advertised":null}'
  fi

  if [ -n "$STATUS_JSON" ] && have python3 && [ "$(tailscale_json_value exit-node 2>/dev/null || printf '0')" = "1" ]; then
    record OK "Exit node advertised: yes." "exit-node-advertised" '{"advertised":true}'
  elif printf '%s\n' "$STATUS_TEXT" | grep -Eqi 'offers exit node|advertise[^[:alnum:]]+exit'; then
    record OK "Exit node advertised: yes." "exit-node-advertised" '{"advertised":true}'
  else
    record OK "Exit node advertised: no." "exit-node-advertised" '{"advertised":false}'
  fi
}

check_tailscale_interface() {
  if [ "$(uname -s)" != "Linux" ] || ! have ip; then
    return 0
  fi

  if ip link 2>/dev/null | grep -Eq 'tailscale[0-9]*|tailscale0'; then
    record OK "Tailscale TUN interface detected." "tailscale-interface" '{"userspace_networking":false}'
  else
    record WARN "No Tailscale TUN interface detected on Linux; userspace networking may hide the actual path from route table checks." "tailscale-interface" '{"userspace_networking":true}'
  fi
}

check_domain_routes() {
  section "Sample domain routes"
  if ! have python3; then
    record FAIL "python3 is required to read $DOMAINS_FILE." "sample-domain-routes"
    return 0
  fi
  if ! have dig && ! have getent; then
    record FAIL "dig or getent is required for DNS checks." "sample-domain-routes"
    return 0
  fi

  local all_domains_tmp
  local domains_tmp
  all_domains_tmp="$(mktemp)"
  domains_tmp="$(mktemp)"
  tmp_files+=("$all_domains_tmp" "$domains_tmp")

  if ! python3 "$ROOT_DIR/scripts/policy_tool.py" domains --domains-file "$DOMAINS_FILE" >"$all_domains_tmp"; then
    record FAIL "Could not read domain file: $DOMAINS_FILE" "sample-domain-routes"
    return 0
  fi

  local wildcard_count
  local sample_count
  wildcard_count="$(awk '/^\*/ { count++ } END { print count + 0 }' "$all_domains_tmp")"
  awk 'NF && !/^\*/ { print; count++; if (count == 5) exit }' "$all_domains_tmp" >"$domains_tmp"
  sample_count="$(awk 'NF { count++ } END { print count + 0 }' "$domains_tmp")"

  if [ "$wildcard_count" -gt 0 ]; then
    record WARN "Skipped $wildcard_count wildcard domain(s); sample route checks require concrete domains." "sample-domain-routes" "{\"wildcards_skipped\":$wildcard_count}"
  fi

  if [ "$sample_count" -eq 0 ]; then
    record FAIL "No non-wildcard AI domains are available for sample route checks." "sample-domain-routes"
    return 0
  fi

  local domain
  local ips
  local ip_addr
  local route_output
  local iface
  local resolved_any=0
  while IFS= read -r domain; do
    [ -n "$domain" ] || continue
    ips="$(resolve_ipv4_all "$domain" || true)"
    if [ -z "$ips" ]; then
      record WARN "$domain could not resolve any IPv4 A records." "sample-domain-routes"
      continue
    fi

    resolved_any=1
    while IFS= read -r ip_addr; do
      [ -n "$ip_addr" ] || continue
      route_output="$(route_get "$ip_addr")"
      iface="$(printf '%s\n' "$route_output" | parse_route_interface)"
      if [ -n "$iface" ]; then
        record OK "$domain -> $ip_addr -> $iface" "sample-domain-routes"
      else
        record WARN "$domain -> $ip_addr route interface could not be determined." "sample-domain-routes"
      fi
    done <<EOF
$ips
EOF
  done <"$domains_tmp"

  if [ "$resolved_any" = "0" ]; then
    record FAIL "No sample AI domains resolved successfully." "sample-domain-routes"
  fi
}

main() {
  parse_args "$@"
  if [ "$JSON_OUTPUT" = "1" ]; then
    have python3 || usage_error "--json requires python3."
    JSON_EVENTS_FILE="$(mktemp)"
    tmp_files+=("$JSON_EVENTS_FILE")
  else
    printf '== VPS-side diagnostics ==\n'
  fi
  record OK "Domain file: $DOMAINS_FILE" "domain-file"
  check_public_egress
  check_forwarding
  check_tailscale
  check_tailscale_modes
  check_tailscale_interface
  check_domain_routes
  if [ "$JSON_OUTPUT" != "1" ]; then
    printf '\nRun ./check-client-routes.sh on a client device to verify client routing.\n'
  fi
  finish
}

main "$@"
