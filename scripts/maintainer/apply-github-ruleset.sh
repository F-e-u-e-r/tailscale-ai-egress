#!/usr/bin/env bash
set -euo pipefail

REPOSITORY="${GITHUB_REPOSITORY:-F-e-u-e-r/tailscale-ai-egress}"
DEFAULT_BRANCH="${DEFAULT_BRANCH:-main}"
RULESET_NAME="${RULESET_NAME:-main-protection}"
REQUIRED_APPROVALS="${REQUIRED_APPROVALS:-0}"
MODE="apply"

usage() {
  cat <<'EOF'
Usage: ./scripts/maintainer/apply-github-ruleset.sh [options]

Create or update the repository ruleset used to protect main. The script also
enables automatic deletion of merged pull-request branches.

Options:
  --repo OWNER/REPO       Repository to configure.
  --branch NAME           Protected branch (default: main).
  --ruleset-name NAME     Managed ruleset name (default: main-protection).
  --required-approvals N  Required approving reviews, 0-10 (default: 0).
  --dry-run               Print the desired ruleset JSON without making API calls.
  --check                 Verify GitHub matches the desired configuration.
  -h, --help              Show this help.

Authentication:
  Run `gh auth login` with repository Administration write permission first.
EOF
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

have() {
  command -v "$1" >/dev/null 2>&1
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo)
      [ -n "${2:-}" ] || die "--repo requires OWNER/REPO."
      REPOSITORY="$2"
      shift 2
      ;;
    --branch)
      [ -n "${2:-}" ] || die "--branch requires a branch name."
      DEFAULT_BRANCH="$2"
      shift 2
      ;;
    --ruleset-name)
      [ -n "${2:-}" ] || die "--ruleset-name requires a name."
      RULESET_NAME="$2"
      shift 2
      ;;
    --required-approvals)
      [ -n "${2:-}" ] || die "--required-approvals requires a number."
      REQUIRED_APPROVALS="$2"
      shift 2
      ;;
    --dry-run)
      MODE="dry-run"
      shift
      ;;
    --check)
      MODE="check"
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

case "$REPOSITORY" in
  */*) ;;
  *) die "--repo must use OWNER/REPO format." ;;
esac

case "$REQUIRED_APPROVALS" in
  ""|*[!0-9]*) die "--required-approvals must be an integer from 0 to 10." ;;
esac
[ "$REQUIRED_APPROVALS" -le 10 ] || die "--required-approvals must be an integer from 0 to 10."

have python3 || die "python3 is required."
if [ "$MODE" != "dry-run" ]; then
  have gh || die "GitHub CLI (gh) is required."
  gh auth status >/dev/null 2>&1 || die "gh is not authenticated. Run 'gh auth login' with repository Administration write permission."
fi

tmp_dir="$(mktemp -d)"
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

payload_file="$tmp_dir/ruleset.json"
RULESET_NAME_VALUE="$RULESET_NAME" \
DEFAULT_BRANCH_VALUE="$DEFAULT_BRANCH" \
REQUIRED_APPROVALS_VALUE="$REQUIRED_APPROVALS" \
python3 - <<'PY' >"$payload_file"
import json
import os
import sys

checks = [
    "test (3.9)",
    "test (3.10)",
    "test (3.11)",
    "test (3.12)",
]

payload = {
    "name": os.environ["RULESET_NAME_VALUE"],
    "target": "branch",
    "enforcement": "active",
    "bypass_actors": [],
    "conditions": {
        "ref_name": {
            "include": [f"refs/heads/{os.environ['DEFAULT_BRANCH_VALUE']}"],
            "exclude": [],
        }
    },
    "rules": [
        {"type": "deletion"},
        {"type": "non_fast_forward"},
        {
            "type": "pull_request",
            "parameters": {
                "allowed_merge_methods": ["merge", "squash", "rebase"],
                "dismiss_stale_reviews_on_push": False,
                "require_code_owner_review": False,
                "require_last_push_approval": False,
                "required_approving_review_count": int(os.environ["REQUIRED_APPROVALS_VALUE"]),
                "required_review_thread_resolution": True,
            },
        },
        {
            "type": "required_status_checks",
            "parameters": {
                "do_not_enforce_on_create": False,
                "required_status_checks": [{"context": check} for check in checks],
                "strict_required_status_checks_policy": True,
            },
        },
    ],
}

json.dump(payload, sys.stdout, indent=2)
print()
PY

if [ "$MODE" = "dry-run" ]; then
  cat "$payload_file"
  printf 'Would set delete_branch_on_merge=true on %s\n' "$REPOSITORY" >&2
  exit 0
fi

gh_api() {
  gh api \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "$@"
}

list_file="$tmp_dir/rulesets.json"
gh_api "repos/$REPOSITORY/rulesets?includes_parents=false&targets=branch&per_page=100" >"$list_file"

ruleset_id="$(
  RULESET_NAME_VALUE="$RULESET_NAME" python3 - "$list_file" <<'PY'
import json
import os
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    rulesets = json.load(handle)

matches = [
    item
    for item in rulesets
    if item.get("name") == os.environ["RULESET_NAME_VALUE"]
    and item.get("source_type", "Repository") == "Repository"
]
if len(matches) > 1:
    raise SystemExit(f"multiple repository rulesets named {os.environ['RULESET_NAME_VALUE']!r}")
if matches:
    print(matches[0]["id"])
PY
)"

verify_configuration() {
  local id="$1"
  local actual_file="$tmp_dir/actual-ruleset.json"
  local repository_file="$tmp_dir/repository.json"

  gh_api "repos/$REPOSITORY/rulesets/$id?includes_parents=false" >"$actual_file"
  gh_api "repos/$REPOSITORY" >"$repository_file"

  python3 - "$payload_file" "$actual_file" "$repository_file" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    desired = json.load(handle)
with open(sys.argv[2], encoding="utf-8") as handle:
    actual = json.load(handle)
with open(sys.argv[3], encoding="utf-8") as handle:
    repository = json.load(handle)

errors = []
for key in ("name", "target", "enforcement", "bypass_actors", "conditions"):
    if actual.get(key) != desired.get(key):
        errors.append(f"ruleset {key} differs")

desired_rules = {item["type"]: item for item in desired["rules"]}
actual_rules = {item["type"]: item for item in actual.get("rules", [])}
if set(actual_rules) != set(desired_rules):
    errors.append("ruleset rule types differ")

for rule_type, desired_rule in desired_rules.items():
    actual_rule = actual_rules.get(rule_type, {})
    desired_parameters = desired_rule.get("parameters")
    if desired_parameters is None:
        continue
    actual_parameters = actual_rule.get("parameters", {})
    if rule_type == "required_status_checks":
        desired_contexts = sorted(item["context"] for item in desired_parameters["required_status_checks"])
        actual_contexts = sorted(item["context"] for item in actual_parameters.get("required_status_checks", []))
        if desired_contexts != actual_contexts:
            errors.append("required status check contexts differ")
        for key in ("do_not_enforce_on_create", "strict_required_status_checks_policy"):
            if actual_parameters.get(key) != desired_parameters[key]:
                errors.append(f"required status checks {key} differs")
    else:
        for key, value in desired_parameters.items():
            if actual_parameters.get(key) != value:
                errors.append(f"{rule_type} {key} differs")

if repository.get("delete_branch_on_merge") is not True:
    errors.append("delete_branch_on_merge is not enabled")

if errors:
    for error in errors:
        print(f"[FAIL] {error}", file=sys.stderr)
    raise SystemExit(1)

print("[OK] GitHub ruleset and merged-branch cleanup match the desired configuration.")
PY
}

if [ "$MODE" = "check" ]; then
  [ -n "$ruleset_id" ] || die "ruleset '$RULESET_NAME' does not exist in $REPOSITORY."
  verify_configuration "$ruleset_id"
  exit 0
fi

result_file="$tmp_dir/result.json"
if [ -n "$ruleset_id" ]; then
  printf 'Updating ruleset %s (%s) in %s...\n' "$RULESET_NAME" "$ruleset_id" "$REPOSITORY"
  gh_api --method PUT "repos/$REPOSITORY/rulesets/$ruleset_id" --input "$payload_file" >"$result_file"
else
  printf 'Creating ruleset %s in %s...\n' "$RULESET_NAME" "$REPOSITORY"
  gh_api --method POST "repos/$REPOSITORY/rulesets" --input "$payload_file" >"$result_file"
  ruleset_id="$(python3 - "$result_file" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle)["id"])
PY
)"
fi

printf 'Enabling automatic deletion of merged pull-request branches...\n'
gh_api --method PATCH "repos/$REPOSITORY" -F delete_branch_on_merge=true >/dev/null
verify_configuration "$ruleset_id"
