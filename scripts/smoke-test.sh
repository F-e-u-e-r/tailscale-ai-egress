#!/usr/bin/env bash
set -euo pipefail

# v1.0 smoke test: exercises the command surface using only local files and
# dry-run paths. It needs no API credentials and makes no network calls, so it
# is safe to run anywhere (CI included). Anything that would touch the network
# or a real tailnet is intentionally excluded.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PY="python3"
TOOL="$PY scripts/policy_tool.py"
ENTRYPOINTS=(bootstrap.sh check-client-routes.sh diagnose.sh enable-exit-node.sh
  disable-exit-node.sh restore-connector.sh rollback.sh install.sh)
FAILS=0

step() {
  printf '\n# %s\n' "$*"
}

run() {
  printf '$ %s\n' "$*"
  if ! "$@" >/dev/null 2>&1; then
    printf 'FAIL: %s\n' "$*" >&2
    FAILS=$((FAILS + 1))
  fi
}

run_json() {
  # Run a command and assert stdout is valid JSON.
  local label="$1"
  shift
  printf '$ %s\n' "$*"
  if ! "$@" 2>/dev/null | "$PY" -m json.tool >/dev/null 2>&1; then
    printf 'FAIL: %s did not produce valid JSON\n' "$label" >&2
    FAILS=$((FAILS + 1))
  fi
}

printf '== tailscale-ai-egress smoke test ==\n'
printf 'Repo: %s\n' "$ROOT_DIR"

step "Versions are consistent and printable"
file_version="$(cat VERSION)"
printf 'VERSION file: %s\n' "$file_version"
run sh -c "$TOOL --version"
for e in "${ENTRYPOINTS[@]}"; do
  out="$(bash "$e" --version)"
  printf '%s\n' "$out"
  case "$out" in
    *" $file_version") ;;
    *)
      printf 'FAIL: %s --version did not report %s\n' "$e" "$file_version" >&2
      FAILS=$((FAILS + 1))
      ;;
  esac
done

step "Help output is stable (exit 0) for every entrypoint"
for e in "${ENTRYPOINTS[@]}"; do
  run sh -c "bash '$e' --help"
done
run sh -c "$TOOL --help"

step "Common domains render"
run sh -c "$TOOL domains --domains-file policy/default-ai-domains.json"

step "Snippet / validate / merge / diff against local files (no network)"
run_json "snippet" sh -c "$TOOL snippet --domains-file policy/default-ai-domains.json"
run sh -c "$TOOL validate --input policy/app-connector.example.json --domains-file policy/default-ai-domains.json"
run_json "merge" sh -c "$TOOL merge --input policy/app-connector.example.json --domains-file policy/default-ai-domains.json"
run sh -c "$TOOL diff --input policy/app-connector.example.json --domains-file policy/default-ai-domains.json"

step "Dry-run bootstrap with common domains (no privileged execution)"
smoke_generated="$(mktemp -d)"
trap 'rm -rf "$smoke_generated"' EXIT
run sh -c "GENERATED_DIR='$smoke_generated' bash bootstrap.sh --dry-run --domain-pack common </dev/null"

if [ "$FAILS" -eq 0 ]; then
  printf '\nSmoke test passed.\n'
  exit 0
fi
printf '\nSmoke test FAILED with %s error(s).\n' "$FAILS" >&2
exit 1
