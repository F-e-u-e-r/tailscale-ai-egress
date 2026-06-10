#!/usr/bin/env bash
set -euo pipefail

# Remote installer wrapper. In a published fork, set TAILSCALE_AI_EGRESS_REPO
# or replace the default below with your GitHub repo URL.
REPO_URL="${TAILSCALE_AI_EGRESS_REPO:-https://github.com/F-e-u-e-r/tailscale-ai-egress}"
BRANCH="${TAILSCALE_AI_EGRESS_BRANCH:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="$(cat "$SCRIPT_DIR/VERSION" 2>/dev/null || true)"
VERSION="${VERSION:-1.1.0}"

if [ "${1:-}" = "--version" ]; then
  printf 'tailscale-ai-egress install.sh %s\n' "$VERSION"
  exit 0
fi

if [ -f "./bootstrap.sh" ] && [ -d "./scripts" ] && [ -d "./policy" ]; then
  ./bootstrap.sh "$@"
  exit $?
fi

if ! command -v curl >/dev/null 2>&1; then
  printf 'error: curl is required before running the remote installer.\n' >&2
  exit 1
fi

if ! command -v tar >/dev/null 2>&1; then
  printf 'error: tar is required before running the remote installer.\n' >&2
  exit 1
fi

sha256_verify() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum -c "$1"
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 -c "$1"
  else
    printf 'error: sha256sum or shasum is required to verify release downloads.\n' >&2
    exit 1
  fi
}

tmp_dir="$(mktemp -d)"
# shellcheck disable=SC2317,SC2329 # Invoked by the EXIT trap.
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

if [ -n "$BRANCH" ]; then
  archive_url="${REPO_URL%/}/archive/refs/heads/${BRANCH}.tar.gz"
  printf 'warning: downloading unverified branch archive %s\n' "$archive_url" >&2
  printf 'For checksum verification, unset TAILSCALE_AI_EGRESS_BRANCH and use a tagged release.\n' >&2
  curl -fsSL "$archive_url" | tar -xz -C "$tmp_dir" --strip-components=1
else
  release_version="${TAILSCALE_AI_EGRESS_VERSION:-$VERSION}"
  release_version="${release_version#v}"
  release_tag="v$release_version"
  asset_name="tailscale-ai-egress-$release_version.tar.gz"
  release_url="${REPO_URL%/}/releases/download/$release_tag"
  archive_path="$tmp_dir/$asset_name"
  sums_path="$tmp_dir/SHA256SUMS"
  selected_sum_path="$tmp_dir/SHA256SUM"

  printf 'Downloading %s/%s\n' "$release_url" "$asset_name"
  curl -fsSLo "$archive_path" "$release_url/$asset_name"
  curl -fsSLo "$sums_path" "$release_url/SHA256SUMS"
  if ! grep "  $asset_name\$" "$sums_path" >"$selected_sum_path"; then
    printf 'error: SHA256SUMS does not contain %s\n' "$asset_name" >&2
    exit 1
  fi
  ( cd "$tmp_dir" && sha256_verify "$selected_sum_path" )
  tar -xzf "$archive_path" -C "$tmp_dir" --strip-components=1
fi

set +e
"$tmp_dir/bootstrap.sh" "$@"
status=$?
set -e
exit "$status"
