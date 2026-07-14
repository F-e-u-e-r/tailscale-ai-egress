#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="$(cat "$ROOT_DIR/VERSION" 2>/dev/null || true)"
VERSION="${VERSION:-1.1.1}"
DOMAIN_PACK="common"
DOMAIN_PACK_SET=0
DOMAINS_FILE=""
BASELINE_DOMAIN="ipinfo.io"
WARN_COUNT=0
FAIL_COUNT=0
JSON_OUTPUT=0
JSON_EVENTS_FILE=""
STATUS_JSON=""
TAILSCALE_INTERFACES=""
USERSPACE_NETWORKING=0
EXIT_NODE_ACTIVE=0
tmp_files=()

usage() {
  cat <<'EOF'
Usage: ./check-client-routes.sh [--domain-pack name] [--domains-file path] [--baseline-domain domain] [--json] [--version]

Client-side route verification for Tailscale AI App Connector setups.

Options:
  --domain-pack name      Use the common domain pack. Default: common.
  --domains-file path     Use a custom domain file. Conflicts with --domain-pack.
  --baseline-domain name  Domain expected to stay local in connector-only mode. Default: ipinfo.io.
  --json                  Emit schema_version=1 JSON instead of text.
  --version               Print the tailscale-ai-egress version and exit.
  -h, --help              Show this help.
EOF
}

usage_error() {
  printf 'error: %s\n' "$*" >&2
  usage >&2
  exit 2
}

show_version() {
  printf 'tailscale-ai-egress check-client-routes.sh %s\n' "$VERSION"
}

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
    "script": "check-client-routes.sh",
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

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --domain-pack)
        [ -n "${2:-}" ] || usage_error "--domain-pack requires a name."
        DOMAIN_PACK="$2"
        DOMAIN_PACK_SET=1
        shift 2
        ;;
      --domains-file)
        [ -n "${2:-}" ] || usage_error "--domains-file requires a path."
        DOMAINS_FILE="$2"
        shift 2
        ;;
      --baseline-domain)
        [ -n "${2:-}" ] || usage_error "--baseline-domain requires a domain."
        BASELINE_DOMAIN="$2"
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

  if [ -n "$DOMAINS_FILE" ] && [ "$DOMAIN_PACK_SET" = "1" ]; then
    usage_error "--domain-pack and --domains-file are mutually exclusive."
  fi

  case "$DOMAIN_PACK" in
    common) ;;
    *) usage_error "Unknown domain pack '$DOMAIN_PACK'. Available packs: common." ;;
  esac
}

pack_domains() {
  # Standalone fallback for when this script is downloaded without policy/*.json.
  # CI compares it against scripts/policy_tool.py domains output.
  case "$1" in
    common)
      cat <<'EOF'
chatgpt.com
*.chatgpt.com
openai.com
*.openai.com
claude.ai
*.claude.ai
anthropic.com
*.anthropic.com
poe.com
*.poe.com
openrouter.ai
*.openrouter.ai
perplexity.ai
*.perplexity.ai
notebooklm.google.com
EOF
      ;;
  esac
}

domain_pack_file() {
  case "$1" in
    common) printf '%s\n' "$ROOT_DIR/policy/default-ai-domains.json" ;;
  esac
}

load_domains_file() {
  if [ ! -f "$DOMAINS_FILE" ]; then
    record FAIL "Domain file not found: $DOMAINS_FILE"
    finish
  fi

  if [ -f "$ROOT_DIR/scripts/policy_tool.py" ] && have python3; then
    python3 "$ROOT_DIR/scripts/policy_tool.py" domains --domains-file "$DOMAINS_FILE"
    return $?
  fi

  if grep -q '^[[:space:]]*\[' "$DOMAINS_FILE"; then
    if have python3; then
      python3 - "$DOMAINS_FILE" <<'PY'
import json
import sys
from pathlib import Path

for item in json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")):
    print(item)
PY
      return $?
    fi
    record FAIL "python3 is required to read JSON domain files without scripts/policy_tool.py."
    finish
  fi

  awk '{
    sub(/#.*/, "")
    gsub(/^[[:space:]]+|[[:space:]]+$/, "")
    if ($0 != "") print tolower($0)
  }' "$DOMAINS_FILE"
}

write_domain_list() {
  local out="$1"
  if [ -n "$DOMAINS_FILE" ]; then
    load_domains_file >"$out" || {
      record FAIL "Could not read domain file: $DOMAINS_FILE"
      finish
    }
  else
    local pack_file
    local saved_domains_file
    pack_file="$(domain_pack_file "$DOMAIN_PACK")"
    if [ -f "$pack_file" ]; then
      saved_domains_file="$DOMAINS_FILE"
      DOMAINS_FILE="$pack_file"
      load_domains_file >"$out" || {
        DOMAINS_FILE="$saved_domains_file"
        record FAIL "Could not read common domain file: $pack_file"
        finish
      }
      DOMAINS_FILE="$saved_domains_file"
    else
      pack_domains "$DOMAIN_PACK" >"$out"
    fi
  fi
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

resolve_ipv6_all() {
  local domain="$1"
  if have dig; then
    dig +short "$domain" AAAA | awk '/:/ { print }'
  elif have getent; then
    getent ahostsv6 "$domain" | awk '{ print $1 }' | awk '!seen[$0]++'
  else
    return 1
  fi
}

route_get6() {
  local ip_addr="$1"
  case "$(uname -s)" in
    Darwin)
      if have route; then
        route -n get -inet6 "$ip_addr" 2>/dev/null || true
      fi
      ;;
    Linux)
      if have ip; then
        ip -6 route get "$ip_addr" 2>/dev/null || true
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

json_python() {
  have python3 || return 1
  python3 -c '
import json
import sys

mode = sys.argv[1]
data = json.load(sys.stdin)

if mode == "exit-node":
    status = data.get("ExitNodeStatus")
    print("1" if isinstance(status, dict) and bool(status.get("ID") or status.get("TailscaleIPs") or status.get("Online")) else "0")
elif mode == "peer-ips":
    peers = data.get("Peer") or {}
    for peer in peers.values():
        for ip in peer.get("TailscaleIPs") or []:
            print(ip)
elif mode == "self-ips":
    for ip in (data.get("Self") or {}).get("TailscaleIPs") or []:
        print(ip)
' "$@"
}

tailscale_json_value() {
  local mode="$1"
  printf '%s\n' "$STATUS_JSON" | json_python "$mode"
}

detect_exit_node() {
  if have python3; then
    EXIT_NODE_ACTIVE="$(tailscale_json_value exit-node 2>/dev/null || printf '0')"
  elif printf '%s\n' "$STATUS_JSON" | grep -q '"ExitNodeStatus"[[:space:]]*:[[:space:]]*{'; then
    EXIT_NODE_ACTIVE=1
  else
    EXIT_NODE_ACTIVE=0
  fi

  if [ "$EXIT_NODE_ACTIVE" = "1" ]; then
    record OK "Exit node appears active; baseline traffic is expected to use the selected exit node for full-traffic mode." "exit-node-active" '{"active":true}'
  else
    record OK "No active exit node detected; baseline traffic should stay local in connector-only mode." "exit-node-active" '{"active":false}'
  fi
}

detect_interfaces_from_routes() {
  have python3 || return 0
  local ip_addr
  local route_output
  local iface
  while IFS= read -r ip_addr; do
    [ -n "$ip_addr" ] || continue
    route_output="$(route_get "$ip_addr")"
    iface="$(printf '%s\n' "$route_output" | parse_route_interface)"
    [ -n "$iface" ] && printf '%s\n' "$iface"
done <<EOF
$(tailscale_json_value peer-ips 2>/dev/null)
EOF
}

detect_interfaces_from_known_links() {
  case "$(uname -s)" in
    Darwin)
      return 0
      ;;
    Linux)
      if have ip; then
        ip -o link show tailscale0 2>/dev/null | awk -F': ' 'NR == 1 { print $2 }'
      elif have ifconfig; then
        ifconfig tailscale0 >/dev/null 2>&1 && printf 'tailscale0\n'
      fi
      ;;
  esac
}

detect_tailscale_interfaces() {
  local tmp
  tmp="$(mktemp)"
  tmp_files+=("$tmp")
  detect_interfaces_from_routes >>"$tmp"
  detect_interfaces_from_known_links >>"$tmp"
  TAILSCALE_INTERFACES="$(awk 'NF && !seen[$0]++ { print }' "$tmp")"

  if [ -n "$TAILSCALE_INTERFACES" ]; then
    record OK "Detected Tailscale route interface(s): $(printf '%s' "$TAILSCALE_INTERFACES" | tr '\n' ' ')" "tailscale-interface"
    return 0
  fi

  if [ "$(uname -s)" = "Linux" ]; then
    if ! have ip || ! ip link show tailscale0 >/dev/null 2>&1; then
      USERSPACE_NETWORKING=1
      record WARN "No Tailscale TUN interface detected on Linux; userspace networking may hide the actual path from route table checks." "tailscale-interface" '{"userspace_networking":true}'
      return 0
    fi
  fi

  record WARN "Could not detect a Tailscale route interface; route checks will use best-effort interface classification." "tailscale-interface"
}

is_detected_tailscale_interface() {
  local iface="$1"
  [ -n "$iface" ] || return 1
  printf '%s\n' "$TAILSCALE_INTERFACES" | grep -Fxq "$iface"
}

classify_interface() {
  local iface="$1"
  if is_detected_tailscale_interface "$iface"; then
    printf 'tailscale\n'
    return 0
  fi

  case "$(uname -s)" in
    Darwin)
      case "$iface" in
        utun*) printf 'possible-tailscale\n'; return 0 ;;
      esac
      ;;
    Linux)
      if [ "$iface" = "tailscale0" ]; then
        printf 'tailscale\n'
        return 0
      fi
      ;;
  esac

  if [ "$USERSPACE_NETWORKING" = "1" ]; then
    printf 'userspace-unknown\n'
  elif [ -z "$iface" ]; then
    printf 'unknown\n'
  else
    printf 'local\n'
  fi
}

route_label() {
  local iface="$1"
  if [ -n "$iface" ]; then
    printf '%s' "$iface"
  else
    printf 'unknown-interface'
  fi
}

emit_domain_results() {
  local role="$1"
  local domain="$2"
  local results="$3"
  local total="$4"
  local tailscale_count="$5"
  local possible_count="$6"
  local unknown_count="$7"
  # id_suffix: "" for IPv4, "-ipv6" for the IPv6 pass -> distinct check ids.
  # ai_unrouted_status: FAIL for IPv4; the IPv6 pass passes "WARN" so an IPv6 mismatch is
  # advisory only and can never flip the script's exit code (missing AAAA is skipped upstream).
  local id_suffix="${8:-}"
  local ai_unrouted_status="${9:-FAIL}"
  local status
  local ip_addr
  local iface
  local class

  if [ "$role" = "ai" ]; then
    if [ "$tailscale_count" -eq "$total" ]; then
      status="OK"
    elif [ $((tailscale_count + possible_count)) -gt 0 ]; then
      status="WARN"
      record WARN "$domain has partial route coverage through App Connector; wait 1-2 minutes and rerun if this was just configured." "ai-route-summary${id_suffix}"
    elif [ "$unknown_count" -eq "$total" ] && [ "$USERSPACE_NETWORKING" = "1" ]; then
      status="WARN"
    else
      status="$ai_unrouted_status"
    fi
  else
    status="OK"
    if [ "$EXIT_NODE_ACTIVE" = "1" ]; then
      if [ $((tailscale_count + possible_count)) -lt "$total" ]; then
        status="WARN"
      fi
    else
      if [ $((tailscale_count + possible_count)) -gt 0 ] || [ "$unknown_count" -gt 0 ]; then
        status="WARN"
      fi
    fi
  fi

  while IFS='|' read -r ip_addr iface class; do
    [ -n "$ip_addr" ] || continue
    case "$role:$status:$class" in
      ai:OK:tailscale)
        record OK "$domain -> $ip_addr -> $(route_label "$iface") via Tailscale App Connector" "ai-domain-route${id_suffix}"
        ;;
      ai:WARN:tailscale)
        record OK "$domain -> $ip_addr -> $(route_label "$iface") via Tailscale App Connector" "ai-domain-route${id_suffix}"
        ;;
      ai:WARN:possible-tailscale)
        record WARN "$domain -> $ip_addr -> $(route_label "$iface") may be Tailscale App Connector; macOS route output is best-effort." "ai-domain-route${id_suffix}"
        ;;
      ai:WARN:userspace-unknown)
        record WARN "$domain -> $ip_addr -> $(route_label "$iface"); Linux userspace networking may hide the path for App Connector." "ai-domain-route${id_suffix}"
        ;;
      ai:WARN:*)
        record WARN "$domain -> $ip_addr -> $(route_label "$iface") is not yet routed through Tailscale App Connector." "ai-domain-route${id_suffix}"
        ;;
      ai:FAIL:*)
        record FAIL "$domain -> $ip_addr -> $(route_label "$iface") is not routed through Tailscale App Connector." "ai-domain-route${id_suffix}"
        ;;
      baseline:OK:tailscale|baseline:OK:possible-tailscale)
        record OK "$domain -> $ip_addr -> $(route_label "$iface") via selected exit node; expected because full-traffic exit-node mode is active." "baseline-route${id_suffix}"
        ;;
      baseline:OK:*)
        record OK "$domain -> $ip_addr -> $(route_label "$iface") normal traffic local." "baseline-route${id_suffix}"
        ;;
      baseline:WARN:tailscale|baseline:WARN:possible-tailscale)
        if [ "$EXIT_NODE_ACTIVE" = "1" ]; then
          record OK "$domain -> $ip_addr -> $(route_label "$iface") via selected exit node; expected because full-traffic exit-node mode is active." "baseline-route${id_suffix}"
        else
          record WARN "$domain -> $ip_addr -> $(route_label "$iface") uses Tailscale; an exit node, broader route, or CDN over-routing may be active." "baseline-route${id_suffix}"
        fi
        ;;
      baseline:WARN:*)
        if [ "$EXIT_NODE_ACTIVE" = "1" ]; then
          record WARN "$domain -> $ip_addr -> $(route_label "$iface") is not using the selected exit node even though exit-node mode appears active." "baseline-route${id_suffix}"
        else
          record OK "$domain -> $ip_addr -> $(route_label "$iface") normal traffic local." "baseline-route${id_suffix}"
        fi
        ;;
    esac
  done <<EOF
$results
EOF

}

check_domain_routes() {
  local role="$1"
  local domain="$2"
  local ips
  local ip_addr
  local route_output
  local iface
  local class
  local results=""
  local total=0
  local tailscale_count=0
  local possible_count=0
  local unknown_count=0

  ips="$(resolve_ipv4_all "$domain" || true)"
  if [ -z "$ips" ]; then
    if [ "$role" = "ai" ]; then
      record FAIL "$domain could not resolve any IPv4 A records." "ai-domain-route"
    else
      record WARN "$domain baseline could not resolve any IPv4 A records." "baseline-route"
    fi
    return 0
  fi

  while IFS= read -r ip_addr; do
    [ -n "$ip_addr" ] || continue
    total=$((total + 1))
    route_output="$(route_get "$ip_addr")"
    iface="$(printf '%s\n' "$route_output" | parse_route_interface)"
    class="$(classify_interface "$iface")"
    results="${results}${ip_addr}|${iface}|${class}
"
    case "$class" in
      tailscale) tailscale_count=$((tailscale_count + 1)) ;;
      possible-tailscale) possible_count=$((possible_count + 1)) ;;
      local) ;;
      *) unknown_count=$((unknown_count + 1)) ;;
    esac
  done <<EOF
$ips
EOF

  emit_domain_results "$role" "$domain" "$results" "$total" "$tailscale_count" "$possible_count" "$unknown_count"
}

check_domain_routes_ipv6() {
  local role="$1"
  local domain="$2"
  local ips
  local ip_addr
  local route_output
  local iface
  local class
  local results=""
  local total=0
  local tailscale_count=0
  local possible_count=0
  local unknown_count=0

  # Advisory IPv6 pass: resolve AAAA once and skip cleanly (no record, never FAIL) when the
  # domain has no AAAA -- many domains are legitimately IPv4-only, and an IPv6 mismatch must
  # not change the script's exit code (the IPv4 pass owns pass/fail).
  ips="$(resolve_ipv6_all "$domain" || true)"
  [ -n "$ips" ] || return 0

  while IFS= read -r ip_addr; do
    [ -n "$ip_addr" ] || continue
    total=$((total + 1))
    route_output="$(route_get6 "$ip_addr")"
    iface="$(printf '%s\n' "$route_output" | parse_route_interface)"
    class="$(classify_interface "$iface")"
    results="${results}${ip_addr}|${iface}|${class}
"
    case "$class" in
      tailscale) tailscale_count=$((tailscale_count + 1)) ;;
      possible-tailscale) possible_count=$((possible_count + 1)) ;;
      local) ;;
      *) unknown_count=$((unknown_count + 1)) ;;
    esac
  done <<EOF
$ips
EOF

  # "-ipv6" id suffix + "WARN" unrouted status: distinct check ids, advisory only (never FAIL).
  emit_domain_results "$role" "$domain" "$results" "$total" "$tailscale_count" "$possible_count" "$unknown_count" "-ipv6" "WARN"
}

main() {
  parse_args "$@"

  if [ "$JSON_OUTPUT" = "1" ]; then
    have python3 || usage_error "--json requires python3."
    JSON_EVENTS_FILE="$(mktemp)"
    tmp_files+=("$JSON_EVENTS_FILE")
  else
    printf '== Client route check ==\n'
  fi

  if ! have tailscale; then
    record FAIL "Tailscale CLI is not installed on this client." "tailscale-cli"
    finish
  fi

  if ! STATUS_JSON="$(tailscale status --json 2>/dev/null)"; then
    record FAIL "Tailscale is not running or this client is not logged in." "tailscale-status"
    finish
  fi

  if have dig || have getent; then
    record OK "DNS lookup tool available." "dns-tool"
  else
    record FAIL "dig or getent is required for DNS checks." "dns-tool"
    finish
  fi

  if tailscale version >/dev/null 2>&1; then
    record OK "Tailscale version: $(tailscale version 2>/dev/null | head -n 1)" "tailscale-version"
  fi

  if [ "$(uname -s)" = "Darwin" ]; then
    record WARN "macOS App Store and standalone Tailscale builds can expose different route table details; this checker uses best-effort route inspection." "macos-route-caveat"
  fi

  detect_exit_node
  detect_tailscale_interfaces

  local domains_tmp
  domains_tmp="$(mktemp)"
  tmp_files+=("$domains_tmp")
  write_domain_list "$domains_tmp"

  if [ "$JSON_OUTPUT" != "1" ]; then
    printf '\n== AI domain routes ==\n'
  fi
  local wildcard_count=0
  local checked_count=0
  while IFS= read -r domain; do
    [ -n "$domain" ] || continue
    case "$domain" in
      \**)
        wildcard_count=$((wildcard_count + 1))
        continue
        ;;
    esac
    checked_count=$((checked_count + 1))
    check_domain_routes ai "$domain"
    check_domain_routes_ipv6 ai "$domain"
  done <"$domains_tmp"
  if [ "$wildcard_count" -gt 0 ]; then
    record WARN "Skipped $wildcard_count wildcard domain(s); route checks require concrete domains." "ai-domain-route" "{\"wildcards_skipped\":$wildcard_count}"
  fi
  if [ "$checked_count" -eq 0 ]; then
    record FAIL "No non-wildcard AI domains are available for route checks." "ai-domain-route"
  fi

  if [ "$JSON_OUTPUT" != "1" ]; then
    printf '\n== Baseline route ==\n'
  fi
  check_domain_routes baseline "$BASELINE_DOMAIN"
  check_domain_routes_ipv6 baseline "$BASELINE_DOMAIN"

  finish
}

if [ "${AI_EGRESS_SOURCE_ONLY:-0}" != "1" ]; then
  main "$@"
fi
