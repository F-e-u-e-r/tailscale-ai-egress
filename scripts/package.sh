#!/usr/bin/env bash
set -euo pipefail

# Build reproducible source release artifacts (tar.gz + zip) and a SHA256SUMS
# file. Packages the current source tree: tracked files plus untracked,
# non-ignored files, honoring .gitignore (so generated/, dist/, caches, and
# secrets are excluded).

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(cat "$ROOT_DIR/VERSION" 2>/dev/null || true)"
VERSION="${VERSION:-1.1.1}"
PREFIX="tailscale-ai-egress-$VERSION"

OUT_DIR="$ROOT_DIR/dist"
CHECK=0

usage() {
  cat <<'EOF'
Usage: ./scripts/package.sh [--output DIR] [--check] [--help]

Build source release artifacts into DIR (default: dist/):
  <prefix>.tar.gz, <prefix>.zip, and SHA256SUMS.

Options:
  --output DIR  Output directory (default: dist/).
  --check       After building, verify SHA256SUMS and required archive contents.
  -h, --help    Show this help.
EOF
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --output)
      [ -n "${2:-}" ] || die "--output requires a directory."
      OUT_DIR="$2"
      shift 2
      ;;
    --check)
      CHECK=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

command -v git >/dev/null 2>&1 || die "git is required."
command -v tar >/dev/null 2>&1 || die "tar is required."
command -v zip >/dev/null 2>&1 || die "zip is required."

sha256_create() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$@"
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$@"
  else
    die "need sha256sum or shasum to generate checksums."
  fi
}

sha256_verify() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum -c "$@"
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 -c "$@"
  else
    die "need sha256sum or shasum to verify checksums."
  fi
}

mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"

staging="$(mktemp -d)"
cleanup() {
  rm -rf "$staging"
}
trap cleanup EXIT

dest="$staging/$PREFIX"
mkdir -p "$dest"

# Copy the file set that represents the source tree.
file_count=0
while IFS= read -r -d '' rel; do
  case "$rel" in
    dist/*) continue ;;
  esac
  [ -e "$ROOT_DIR/$rel" ] || continue
  mkdir -p "$dest/$(dirname "$rel")"
  cp "$ROOT_DIR/$rel" "$dest/$rel"
  file_count=$((file_count + 1))
done < <(git -C "$ROOT_DIR" ls-files --cached --others --exclude-standard -z)

[ "$file_count" -gt 0 ] || die "no files to package."

tar -czf "$OUT_DIR/$PREFIX.tar.gz" -C "$staging" "$PREFIX"
( cd "$staging" && zip -qr "$OUT_DIR/$PREFIX.zip" "$PREFIX" )
( cd "$OUT_DIR" && sha256_create "$PREFIX.tar.gz" "$PREFIX.zip" > SHA256SUMS )

printf 'Built %s files into:\n' "$file_count"
printf '  %s/%s.tar.gz\n' "$OUT_DIR" "$PREFIX"
printf '  %s/%s.zip\n' "$OUT_DIR" "$PREFIX"
printf '  %s/SHA256SUMS\n' "$OUT_DIR"

if [ "$CHECK" = "1" ]; then
  printf '\nVerifying checksums...\n'
  ( cd "$OUT_DIR" && sha256_verify SHA256SUMS )

  printf 'Verifying archive contents...\n'
  verify_dir="$(mktemp -d)"
  tar -xzf "$OUT_DIR/$PREFIX.tar.gz" -C "$verify_dir"
  required=(VERSION LICENSE README.md SECURITY.md PRIVACY.md CHANGELOG.md
    bootstrap.sh check-client-routes.sh diagnose.sh enable-exit-node.sh
    disable-exit-node.sh failover-exit-node.sh monitor-connectors.sh
    restore-connector.sh rollback.sh install.sh
    policy/default-ai-domains.json scripts/policy_tool.py
    scripts/health_check.py examples/failover.env.example
    scripts/validation-e2e.sh scripts/lib/common.sh)
  missing=""
  for req in "${required[@]}"; do
    [ -f "$verify_dir/$PREFIX/$req" ] || missing="$missing $req"
  done
  # Ensure ignored/sensitive paths did not leak into the archive.
  leaked=""
  for bad in generated/policy-plans dist .git; do
    [ -e "$verify_dir/$PREFIX/$bad" ] && leaked="$leaked $bad"
  done
  rm -rf "$verify_dir"
  [ -z "$missing" ] || die "archive is missing required file(s):$missing"
  [ -z "$leaked" ] || die "archive unexpectedly contains:$leaked"
  printf 'Packaging check passed.\n'
fi
