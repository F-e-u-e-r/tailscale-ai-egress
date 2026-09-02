#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="$(cat "$ROOT_DIR/VERSION" 2>/dev/null || true)"
VERSION="${VERSION:-1.3.0}"
GENERATED_DIR="${GENERATED_DIR:-$ROOT_DIR/generated}"
TAILSCALE_TAILNET="${TAILSCALE_TAILNET:--}"
DRY_RUN="${DRY_RUN:-0}"
ROLLBACK_ACK="${ROLLBACK_ACK:-0}"
LIST_BACKUPS=0

usage() {
  cat <<'EOF'
Usage: ./rollback.sh [--dry-run] [--list] [--version] [backup-file]

Restore a saved tailnet policy backup. If no backup file is provided, the latest
`generated/tailnet-policy.backup.*.hujson` file is selected. Use --list to show
available backups before choosing one.

With TAILSCALE_API_KEY or TAILSCALE_OAUTH_CLIENT_ID/SECRET set, this validates
and restores through the Tailscale API. Without API credentials, it prints the
backup path and Admin Console URL for manual rollback.

Environment:
  GENERATED_DIR=/path/to/generated
  TAILSCALE_TAILNET=-
  TAILSCALE_API_KEY=tskey-api-...
  TAILSCALE_OAUTH_CLIENT_ID=...
  TAILSCALE_OAUTH_CLIENT_SECRET=...
  ROLLBACK_ACK=1   Skip the interactive restore confirmation.
EOF
}

show_version() {
  printf 'tailscale-ai-egress rollback.sh %s\n' "$VERSION"
}

have_policy_credential() {
  [ -n "${TAILSCALE_API_KEY:-}" ] ||
    { [ -n "${TAILSCALE_OAUTH_CLIENT_ID:-}" ] && [ -n "${TAILSCALE_OAUTH_CLIENT_SECRET:-}" ]; }
}

is_interactive() {
  [ -t 0 ] || ( : </dev/tty ) 2>/dev/null
}

read_answer() {
  local prompt="$1"
  local answer=""
  if [ -t 0 ]; then
    printf '%s' "$prompt"
    IFS= read -r answer || true
  elif ( : </dev/tty ) 2>/dev/null; then
    printf '%s' "$prompt" >/dev/tty
    IFS= read -r answer </dev/tty || true
  else
    return 1
  fi
  printf '%s' "$answer"
}

list_backups() {
  local found=0
  while IFS= read -r file; do
    found=1
    local name stamp
    name="${file##*/}"
    stamp="${name#tailnet-policy.backup.}"
    stamp="${stamp%.hujson}"
    printf '%s  %s\n' "$stamp" "$file"
  done < <(find "$GENERATED_DIR" -maxdepth 1 -name 'tailnet-policy.backup.*.hujson' -type f 2>/dev/null | sort)

  if [ "$found" -eq 0 ]; then
    printf 'No backup files found in %s\n' "$GENERATED_DIR" >&2
    exit 1
  fi
}

confirm_restore() {
  printf 'About to restore: %s\n' "$backup"
  printf 'This will replace the current tailnet policy for tailnet %s.\n' "$TAILSCALE_TAILNET"

  if [ "$DRY_RUN" = "1" ]; then
    printf 'Dry run enabled; the backup will be validated but not restored.\n'
    return 0
  fi

  if [ "$ROLLBACK_ACK" = "1" ]; then
    printf 'ROLLBACK_ACK=1 set; skipping interactive confirmation.\n'
    return 0
  fi

  if ! is_interactive; then
    printf 'error: refusing to restore non-interactively without ROLLBACK_ACK=1\n' >&2
    exit 1
  fi

  local answer
  answer="$(read_answer 'Continue? [y/N] ' || true)"
  case "$answer" in
    y|Y|yes|YES) ;;
    *)
      printf 'Rollback cancelled.\n'
      exit 1
      ;;
  esac
}

backup=""
backup_provided=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --list)
      LIST_BACKUPS=1
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
    --*)
      printf 'error: unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
    *)
      if [ -n "$backup" ]; then
        printf 'error: only one backup file may be provided\n' >&2
        usage >&2
        exit 2
      fi
      backup="$1"
      backup_provided=1
      shift
      ;;
  esac
done

if [ "$LIST_BACKUPS" = "1" ]; then
  if [ -n "$backup" ]; then
    printf 'error: --list does not accept a backup file\n' >&2
    usage >&2
    exit 2
  fi
  list_backups
  exit 0
fi

if [ -z "$backup" ]; then
  backup="$(find "$GENERATED_DIR" -maxdepth 1 -name 'tailnet-policy.backup.*.hujson' -type f 2>/dev/null | sort | tail -n 1)"
fi

if [ -n "$backup" ] && [ ! -f "$backup" ] && [ "$backup_provided" = "1" ]; then
  printf 'error: backup not found: %s\n' "$backup" >&2
  exit 1
fi

if [ -z "$backup" ] || [ ! -f "$backup" ]; then
  printf 'error: no backup file found. Pass one explicitly or check %s\n' "$GENERATED_DIR" >&2
  exit 1
fi

if ! have_policy_credential; then
  printf 'No Tailscale API/OAuth credential found. Restore manually:\n\n'
  printf '  Backup: %s\n' "$backup"
  printf '  Admin Console: https://login.tailscale.com/admin/acls/file\n'
  exit 0
fi

confirm_restore

restore_args=(
  restore
  --input "$backup"
  --tailnet "$TAILSCALE_TAILNET"
  --backup-dir "$GENERATED_DIR"
)
if [ "$DRY_RUN" = "1" ]; then
  restore_args+=(--dry-run)
fi

python3 "$ROOT_DIR/scripts/policy_tool.py" "${restore_args[@]}"
