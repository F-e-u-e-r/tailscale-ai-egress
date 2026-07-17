#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(cat "$ROOT_DIR/VERSION" 2>/dev/null || true)"
VERSION="${VERSION:-1.2.0}"

VPS_SSH="${VPS_SSH:-local}"
CLIENT_SSH="${CLIENT_SSH:-local}"
VPS_REPO_DIR="${VPS_REPO_DIR:-$ROOT_DIR}"
CLIENT_REPO_DIR="${CLIENT_REPO_DIR:-$ROOT_DIR}"
VALIDATION_OUT_DIR="${VALIDATION_OUT_DIR:-$ROOT_DIR/generated/validation/$(date -u +%Y%m%dT%H%M%SZ)}"

usage() {
  cat <<'EOF'
Usage: ./scripts/validation-e2e.sh [--version] [--help]

Collect real-environment validation evidence from a connector host and a client.
By default both commands run locally. Set VPS_SSH and/or CLIENT_SSH to run over
SSH, and set VPS_REPO_DIR / CLIENT_REPO_DIR if the repo lives elsewhere.

Environment:
  VPS_SSH=user@connector-host   # or local
  CLIENT_SSH=user@client-host   # or local
  VPS_REPO_DIR=/path/to/repo
  CLIENT_REPO_DIR=/path/to/repo
  VALIDATION_OUT_DIR=generated/validation/<run-id>
EOF
}

show_version() {
  printf 'tailscale-ai-egress validation-e2e.sh %s\n' "$VERSION"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --version)
      show_version
      exit 0
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'error: unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

quote_words() {
  local word
  for word in "$@"; do
    printf '%q ' "$word"
  done
}

run_in_repo() {
  local target="$1"
  local repo_dir="$2"
  shift 2

  if [ "$target" = "local" ] || [ -z "$target" ]; then
    ( cd "$repo_dir" && "$@" )
    return $?
  fi

  local remote_cmd
  remote_cmd="cd $(quote_words "$repo_dir")&& $(quote_words "$@")"
  # shellcheck disable=SC2029 # Command words are intentionally quoted locally before SSH execution.
  ssh "$target" "$remote_cmd"
}

capture_json() {
  local name="$1"
  local target="$2"
  local repo_dir="$3"
  shift 3

  local output="$VALIDATION_OUT_DIR/$name.json"
  local status_file="$VALIDATION_OUT_DIR/$name.status"

  printf 'Running %s on %s\n' "$name" "$target"
  set +e
  run_in_repo "$target" "$repo_dir" "$@" >"$output"
  local status=$?
  set -e
  printf '%s\n' "$status" >"$status_file"
  if [ "$status" -ne 0 ]; then
    printf 'warning: %s exited with status %s; evidence kept at %s\n' "$name" "$status" "$output" >&2
  fi
  return "$status"
}

mkdir -p "$VALIDATION_OUT_DIR"

status=0
capture_json "connector-diagnose" "$VPS_SSH" "$VPS_REPO_DIR" ./diagnose.sh --json || status=1
capture_json "client-routes" "$CLIENT_SSH" "$CLIENT_REPO_DIR" ./check-client-routes.sh --json || status=1

cat >"$VALIDATION_OUT_DIR/README.txt" <<EOF
Validation evidence run
Created: $(date -u +%Y-%m-%dT%H:%M:%SZ)
Connector target: $VPS_SSH
Connector repo: $VPS_REPO_DIR
Client target: $CLIENT_SSH
Client repo: $CLIENT_REPO_DIR

Files:
- connector-diagnose.json
- connector-diagnose.status
- client-routes.json
- client-routes.status
EOF

printf 'Validation evidence written to %s\n' "$VALIDATION_OUT_DIR"
exit "$status"
