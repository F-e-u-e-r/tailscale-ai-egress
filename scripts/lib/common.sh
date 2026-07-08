# shellcheck shell=bash
# scripts/lib/common.sh -- shared internal shell library (NOT a CLI entrypoint).
#
# Status: MIGRATION IN PROGRESS. Helpers move here one at a time, each proven by
# a parity test before a consumer switches to it. See
# docs/design/shared-shell-library.md for the migration plan.
# Implemented so far: ai_egress_run_root (consumer: enable-exit-node.sh). The
# remaining families listed below are still inline in their scripts.
#
# This library is deliberately NOT part of the frozen 1.x CLI surface described
# in docs/Stability.md: it has no --version, no flags, and no JSON schema. It is
# an internal implementation detail that entrypoints may source.
#
# Motivation: read_line/read_secret, run_root, and connector-identity resolution
# are near-duplicated across bootstrap.sh, enable-exit-node.sh, and
# restore-connector.sh (~150 lines each). The v1.1.1 read_line/dev-tty fix had to
# be applied in three places because the same bug existed in all three copies.
# Centralizing these helpers removes that "fix-one-forget-three" hazard.
#
# Usage (once helpers land): source it from an entrypoint; never execute it.
#   . "$SCRIPT_DIR/scripts/lib/common.sh"
#
# Naming: every public helper is namespaced ai_egress_* so it cannot clobber an
# entrypoint-local function during incremental migration, and so a consumer can
# adopt one helper at a time without a big-bang cutover.

# Refuse direct execution: this is a source-only library, not a command. The
# guard is Bash 3.2 compatible (BASH_SOURCE exists since Bash 3.0) so it works on
# stock macOS as well as Linux.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  printf 'scripts/lib/common.sh is a source-only library, not a command; source it instead of executing it.\n' >&2
  exit 64 # EX_USAGE
fi

# ai_egress_run_root <cmd...> -- run a command with privilege, honoring the
# caller's DRY_RUN and USE_SUDO. With DRY_RUN=1 it prints the command (prefixed
# with `+`, and `sudo` when it would escalate) using %q quoting and does not
# execute. Otherwise it runs directly when root or USE_SUDO=0, else via sudo.
# Behavior is byte-identical to the inline run_root it replaces (parity-tested).
# DRY_RUN and USE_SUDO are provided by the sourcing script's scope.
ai_egress_run_root() {
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

# --- Intended helper families (bodies land later, with parity tests) ----------
# Nothing below is implemented yet; these are the reserved names and contracts.
# Each will move here from its current inline copies only once a parity test
# proves the extracted version matches the original for the migrated consumer.
#
#   ai_egress_have <cmd>                -- command -v presence check.
#   ai_egress_note / _warn / _die       -- consistent stdout/stderr logging.
#   ai_egress_read_line <prompt>        -- prompt + read with the /dev/tty
#                                          fallback that prints the prompt to the
#                                          tty (bootstrap / enable / restore).
#   ai_egress_read_secret <prompt>      -- silent variant of read_line for auth
#                                          keys (no echo; newline to the tty).
#   ai_egress_resolve_identity          -- connector tag/hostname resolution from
#                                          environment, the persisted identity
#                                          file, and `tailscale status` (note:
#                                          enable/restore resolve slightly
#                                          different field sets; the migration
#                                          must preserve each).
#
# Do not add real bodies here without also migrating a consumer and landing the
# corresponding parity test in the same change (see the design doc). Dead-but-real
# helper code tends to rot or lock in the wrong signature.
