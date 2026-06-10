import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/maintainer/apply-github-ruleset.sh"


class GitHubRulesetScriptTests(unittest.TestCase):
    def _fake_gh_env(self, tmp):
        tmp_path = Path(tmp)
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        state = tmp_path / "ruleset.json"
        log = tmp_path / "gh.log"
        gh = fake_bin / "gh"
        gh.write_text(
            """#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\\n")

if args[:2] == ["auth", "status"]:
    raise SystemExit(0)
if not args or args[0] != "api":
    raise SystemExit("unexpected gh command")

method = "GET"
if "--method" in args:
    method = args[args.index("--method") + 1]
endpoint = next((arg for arg in args[1:] if arg.startswith("repos/")), "")
state_file = os.environ["FAKE_GH_STATE"]

if endpoint.endswith("rulesets?includes_parents=false&targets=branch&per_page=100"):
    if os.path.exists(state_file):
        with open(state_file, encoding="utf-8") as handle:
            payload = json.load(handle)
        print(json.dumps([{"id": payload["id"], "name": payload["name"], "source_type": "Repository"}]))
    else:
        print("[]")
elif endpoint.endswith("rulesets") and method == "POST":
    input_file = args[args.index("--input") + 1]
    with open(input_file, encoding="utf-8") as handle:
        payload = json.load(handle)
    payload["id"] = 42
    with open(state_file, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    print(json.dumps(payload))
elif endpoint.endswith("rulesets/42") and method == "PUT":
    input_file = args[args.index("--input") + 1]
    with open(input_file, encoding="utf-8") as handle:
        payload = json.load(handle)
    payload["id"] = 42
    with open(state_file, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    print(json.dumps(payload))
elif endpoint.endswith("rulesets/42?includes_parents=false"):
    with open(state_file, encoding="utf-8") as handle:
        print(handle.read())
elif endpoint == "repos/example/project" and method == "PATCH":
    print('{"delete_branch_on_merge":true}')
elif endpoint == "repos/example/project":
    print('{"delete_branch_on_merge":true}')
else:
    raise SystemExit(f"unexpected endpoint: {method} {endpoint}")
""",
            encoding="utf-8",
        )
        gh.chmod(0o755)

        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
        env["FAKE_GH_STATE"] = str(state)
        env["FAKE_GH_LOG"] = str(log)
        return env, log

    def test_dry_run_emits_canonical_main_ruleset(self):
        result = subprocess.run(
            ["bash", str(SCRIPT), "--dry-run"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )

        payload = json.loads(result.stdout)
        self.assertEqual(payload["name"], "main-protection")
        self.assertEqual(payload["target"], "branch")
        self.assertEqual(payload["enforcement"], "active")
        self.assertEqual(payload["conditions"]["ref_name"]["include"], ["refs/heads/main"])

        rules = {item["type"]: item for item in payload["rules"]}
        self.assertIn("deletion", rules)
        self.assertIn("non_fast_forward", rules)
        self.assertEqual(rules["pull_request"]["parameters"]["required_approving_review_count"], 0)
        self.assertTrue(rules["pull_request"]["parameters"]["required_review_thread_resolution"])
        self.assertTrue(rules["required_status_checks"]["parameters"]["strict_required_status_checks_policy"])
        self.assertEqual(
            [item["context"] for item in rules["required_status_checks"]["parameters"]["required_status_checks"]],
            ["test (3.9)", "test (3.10)", "test (3.11)", "test (3.12)"],
        )
        self.assertIn("delete_branch_on_merge=true", result.stderr)

    def test_dry_run_accepts_documented_overrides(self):
        result = subprocess.run(
            [
                "bash",
                str(SCRIPT),
                "--repo",
                "example/project",
                "--branch",
                "stable",
                "--ruleset-name",
                "stable-protection",
                "--required-approvals",
                "1",
                "--dry-run",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )

        payload = json.loads(result.stdout)
        self.assertEqual(payload["name"], "stable-protection")
        self.assertEqual(payload["conditions"]["ref_name"]["include"], ["refs/heads/stable"])
        rules = {item["type"]: item for item in payload["rules"]}
        self.assertEqual(rules["pull_request"]["parameters"]["required_approving_review_count"], 1)
        self.assertIn("example/project", result.stderr)

    def test_rejects_invalid_approval_count(self):
        result = subprocess.run(
            ["bash", str(SCRIPT), "--required-approvals", "eleven", "--dry-run"],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("integer from 0 to 10", result.stderr)

    def test_apply_is_idempotent_and_checkable(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log = self._fake_gh_env(tmp)

            created = subprocess.run(
                ["bash", str(SCRIPT), "--repo", "example/project"],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            checked = subprocess.run(
                ["bash", str(SCRIPT), "--repo", "example/project", "--check"],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            updated = subprocess.run(
                ["bash", str(SCRIPT), "--repo", "example/project"],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

        self.assertIn("Creating ruleset", created.stdout)
        self.assertIn("[OK]", created.stdout)
        self.assertIn("[OK]", checked.stdout)
        self.assertIn("Updating ruleset", updated.stdout)
        self.assertTrue(any("--method" in call and "POST" in call for call in calls))
        self.assertTrue(any("--method" in call and "PUT" in call for call in calls))
        self.assertTrue(any("--method" in call and "PATCH" in call for call in calls))


if __name__ == "__main__":
    unittest.main()
