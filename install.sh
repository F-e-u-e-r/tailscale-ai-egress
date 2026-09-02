#!/usr/bin/env bash
set -euo pipefail

# Remote installer wrapper. In a published fork, set TAILSCALE_AI_EGRESS_REPO
# or replace the default below with your GitHub repo URL.
REPO_URL="${TAILSCALE_AI_EGRESS_REPO:-https://github.com/F-e-u-e-r/tailscale-ai-egress}"
BRANCH="${TAILSCALE_AI_EGRESS_BRANCH:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="$(cat "$SCRIPT_DIR/VERSION" 2>/dev/null || true)"
VERSION="${VERSION:-1.3.0}"

if [ "${1:-}" = "--version" ]; then
  printf 'tailscale-ai-egress install.sh %s\n' "$VERSION"
  exit 0
fi

if [ -f "./bootstrap.sh" ] && [ -d "./scripts" ] && [ -d "./policy" ]; then
  printf 'install.sh: executing local checkout in %s (./bootstrap.sh)\n' "$(pwd)" >&2
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

# Return 0 only for a gh new enough to run `attestation verify` safely.
# gh <= 2.92.0 can leak the auth token to a TUF mirror (GHSA-8xvp-7hj6-mcj9) and
# 2.49-2.66 can false-pass a mismatched predicate (GHSA-fgw4-v983-mgp8); 2.93.0
# is the first release patched against both. Parse ONLY a bare numeric
# MAJOR.MINOR.PATCH token from `gh version X.Y.Z (...)`; a dev/prerelease/+build
# suffix, any non-numeric output, or a failed `gh --version` is unrecognized and
# returns non-zero (caller then SKIPs, never invoking the vulnerable command).
gh_attestation_supported() {
  local out line ver major minor patch
  # GH_TELEMETRY=false: this project sends no telemetry, so do not let gh phone
  # home on our behalf either (gh telemetry defaults on since 2.91.0).
  # GH_NO_UPDATE_NOTIFIER=1: gh separately checks for its own new releases once
  # every 24h on ANY command; PRIVACY.md promises "no update check", so disable
  # that phone-home too on every gh invocation.
  out="$(GH_TELEMETRY=false GH_NO_UPDATE_NOTIFIER=1 gh --version 2>/dev/null)" || return 1
  line="${out%%$'\n'*}"
  case "$line" in
    'gh version '*) ver="${line#gh version }" ;;
    *) return 1 ;;
  esac
  ver="${ver%% *}"
  case "$ver" in *[!0-9.]*|'') return 1 ;; esac
  IFS=. read -r major minor patch _ <<<"$ver"
  # Exactly three non-empty numeric components: the reconstruction rejects a
  # trailing dot / 4th component (e.g. `2.93.0.`) that a bare empty-`rest` check
  # would let through; the char class above guarantees each part is numeric.
  [ -n "$major" ] && [ -n "$minor" ] && [ -n "$patch" ] \
    && [ "$ver" = "$major.$minor.$patch" ] || return 1
  major=$((10#$major)); minor=$((10#$minor))
  if [ "$major" -gt 2 ]; then return 0; fi
  if [ "$major" -eq 2 ] && [ "$minor" -ge 93 ]; then return 0; fi
  return 1
}

# Print `owner/repo` for an https://github.com/<owner>/<repo> URL (optional
# trailing slash or `.git`), else return non-zero. HTTPS-only because REPO_URL is
# composed directly into the HTTPS download URLs above, so no SSH/scp form is
# reachable here. Must return explicitly: it is called from a `||` list.
derive_repo_slug() {
  local url="$1" rest owner repo
  case "$url" in
    https://github.com/*) rest="${url#https://github.com/}" ;;
    *) return 1 ;;
  esac
  rest="${rest%/}"
  rest="${rest%.git}"
  case "$rest" in */*) : ;; *) return 1 ;; esac
  owner="${rest%%/*}"
  repo="${rest#*/}"
  case "$owner" in ''|*/*) return 1 ;; esac
  case "$repo"  in ''|*/*) return 1 ;; esac
  printf '%s/%s\n' "$owner" "$repo"
  return 0
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

  # Publisher provenance (optional; additive to the SHA256SUMS checksum above).
  # When a safe gh is present, verify the release tarball's build attestation,
  # narrowed to this repo's release workflow on the exact tag. gh absent / too
  # old / unauthenticated / opt-out all degrade to checksum-only; only an actual
  # verify failure by an authenticated gh is fatal, mirroring the checksum's
  # fail-closed posture.
  if [ "${TAILSCALE_AI_EGRESS_SKIP_ATTESTATION:-0}" = "1" ]; then
    printf 'note: attestation verification skipped (TAILSCALE_AI_EGRESS_SKIP_ATTESTATION=1); the SHA256SUMS checksum was verified.\n'
  elif ! command -v gh >/dev/null 2>&1; then
    printf 'note: gh not found; verified the SHA256SUMS checksum but not publisher provenance. Install GitHub CLI >= 2.93.0 to also verify attestations.\n'
  elif ! gh_attestation_supported; then
    printf 'note: gh is older than 2.93.0 (or its version is unrecognized); skipping attestation verification to avoid a known token-leak/false-pass issue. Upgrade gh, or set TAILSCALE_AI_EGRESS_SKIP_ATTESTATION=1 to silence this. The SHA256SUMS checksum was verified.\n'
  else
    repo_slug="$(derive_repo_slug "$REPO_URL")" \
      || { printf 'error: could not derive an https://github.com owner/repo slug from %s; set TAILSCALE_AI_EGRESS_SKIP_ATTESTATION=1 for a non-GitHub mirror.\n' "$REPO_URL" >&2; exit 1; }
    printf 'Verifying release attestation with gh (repo %s) ...\n' "$repo_slug"
    attest_rc=0
    GH_TELEMETRY=false GH_NO_UPDATE_NOTIFIER=1 gh attestation verify "$archive_path" \
          --repo "$repo_slug" \
          --signer-workflow "$repo_slug/.github/workflows/release.yml" \
          --source-ref "refs/tags/$release_tag" \
          --hostname github.com || attest_rc=$?
    if [ "$attest_rc" -eq 4 ]; then
      # gh's documented "requires authentication" exit code (`gh help exit-codes`):
      # `gh attestation verify` needs a github.com credential even for public
      # repos, and a fresh host often has gh installed but never `gh auth login`ed.
      # Inability to check is not a rejection -> degrade to checksum-only, loudly.
      printf 'note: gh is not authenticated for github.com, so publisher provenance was not verified. Run "gh auth login" (or set GH_TOKEN) to enable attestation verification, or set TAILSCALE_AI_EGRESS_SKIP_ATTESTATION=1 to silence this note. The SHA256SUMS checksum was verified.\n'
    elif [ "$attest_rc" -ne 0 ]; then
      printf 'error: attestation verification failed for %s\n' "$asset_name" >&2
      exit 1
    fi
  fi

  tar -xzf "$archive_path" -C "$tmp_dir" --strip-components=1
fi

set +e
"$tmp_dir/bootstrap.sh" "$@"
status=$?
set -e
exit "$status"
