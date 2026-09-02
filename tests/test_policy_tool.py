import argparse
import copy
import datetime as dt
import io
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import policy_tool as tool


class PolicyToolTests(unittest.TestCase):
    def run_policy_tool(self, *args):
        return subprocess.run(
            ["python3", str(ROOT / "scripts/policy_tool.py"), *args],
            text=True,
            capture_output=True,
        )

    def test_strip_hujson_comments_and_trailing_commas(self):
        text = '{\n          // comment\n          "grants": [\n            {"src": ["*"], "dst": ["*"], "ip": ["*"],},\n          ],\n        }'
        parsed = tool.parse_policy(text)
        self.assertEqual(parsed["grants"][0]["src"], ["*"])

    def test_domain_validation_accepts_punycode_tld(self):
        self.assertEqual(
            tool.normalize_domains(["xn--e1afmapc.xn--p1ai", "example.xn--3e0b707e"]),
            ["xn--e1afmapc.xn--p1ai", "example.xn--3e0b707e"],
        )
        with self.assertRaises(tool.PolicyError):
            tool.normalize_domains(["example.123"])

    def test_merge_preserves_existing_connector_domains(self):
        policy = {
            "nodeAttrs": [
                {
                    "target": ["*"],
                    "app": {
                        tool.APP_CONNECTORS_KEY: [
                            {
                                "name": "AI-Egress-JP",
                                "connectors": ["tag:old"],
                                "domains": ["manual.example.com"],
                            }
                        ]
                    },
                }
            ]
        }
        merged = tool.merge_policy(
            policy,
            connector_name="AI-Egress-JP",
            connector_tag="tag:ai-egress-jp",
            domains=["chatgpt.com"],
            tag_owner="autogroup:admin",
            member_src="autogroup:member",
        )
        connector = merged["nodeAttrs"][0]["app"][tool.APP_CONNECTORS_KEY][0]
        self.assertEqual(connector["connectors"], ["tag:old", "tag:ai-egress-jp"])
        self.assertEqual(connector["domains"], ["manual.example.com", "chatgpt.com"])

    def test_strip_hujson_keeps_comment_markers_inside_strings(self):
        text = r'''{
          "url": "https://example.com/path//still-string",
          "note": "block /* marker */ stays inside the string",
        }'''
        parsed = tool.parse_policy(text)
        self.assertEqual(parsed["url"], "https://example.com/path//still-string")
        self.assertEqual(parsed["note"], "block /* marker */ stays inside the string")

    def test_parse_policy_reports_unterminated_hujson_comment(self):
        with self.assertRaisesRegex(tool.PolicyError, "Unterminated block comment"):
            tool.parse_policy('{"tagOwners": {} /* missing close')

    def test_redact_sensitive_masks_tokens_and_authorization_headers(self):
        text = "Authorization: Bearer tskey-api-secret and authorization='Basic abc123'"
        redacted = tool.redact_sensitive(text)
        self.assertNotIn("tskey-api-secret", redacted)
        self.assertNotIn("abc123", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_normalize_domains_dedupes_trailing_dot_and_case(self):
        self.assertEqual(
            tool.normalize_domains(["ChatGPT.COM.", "chatgpt.com", "*.CHATGPT.com."]),
            ["chatgpt.com", "*.chatgpt.com"],
        )

    def test_connector_validation_rejects_invalid_name_and_tag(self):
        _domains, findings = tool.validate_connector_config(
            connector_name="Bad Name",
            connector_tag="tag:Bad_Tag",
            raw_domains=["chatgpt.com"],
        )
        failed_ids = {item["id"] for item in findings if item["status"] == "fail"}
        self.assertEqual(failed_ids, {"connector-name", "connector-tag"})

    def test_broad_wildcard_rejection_and_override(self):
        with self.assertRaisesRegex(tool.PolicyError, "Broad wildcard"):
            tool.normalize_domains(["*.com"])
        self.assertEqual(tool.normalize_domains(["*.com"], allow_broad_wildcard=True), ["*.com"])

    def test_broad_cdn_domains_warn_but_are_kept(self):
        # A CDN / shared-infra domain WARNS (never blocks) and is still returned. Both the
        # bare `cdn.net` and the `*.cdn.net` wildcard form match, case-insensitively.
        for entry in ("cloudfront.net", "*.cloudfront.net", "*.CloudFront.net"):
            with self.subTest(entry=entry):
                fs: list = []
                result = tool.normalize_domains([entry], findings=fs)
                self.assertEqual(result, [entry.lower()])   # kept, not dropped or raised
                self.assertTrue(any(f["id"] == "broad-wildcard-warning" and f["status"] == "warn" for f in fs))
        # a non-CDN domain produces no such warning
        fs2: list = []
        tool.normalize_domains(["chatgpt.com"], findings=fs2)
        self.assertFalse(any(f["id"] == "broad-wildcard-warning" for f in fs2))
        # several warned domains produce EXACTLY ONE aggregate finding listing them all
        # (guards against a per-domain-finding mutant).
        fs3: list = []
        tool.normalize_domains(["*.cloudfront.net", "amazonaws.com", "chatgpt.com"], findings=fs3)
        warns = [f for f in fs3 if f["id"] == "broad-wildcard-warning"]
        self.assertEqual(len(warns), 1)
        self.assertEqual(warns[0]["details"]["domains"], ["*.cloudfront.net", "amazonaws.com"])
        # the shipped default domain pack must never trip the broad-wildcard warning.
        default_domains = json.loads((ROOT / "policy" / "default-ai-domains.json").read_text(encoding="utf-8"))
        fs4: list = []
        tool.normalize_domains(default_domains, findings=fs4)
        self.assertFalse(any(f["id"] == "broad-wildcard-warning" for f in fs4))

    def test_merge_empty_policy_builds_required_fields(self):
        merged = tool.merge_policy(
            {},
            connector_name="AI-Egress-JP",
            connector_tag="tag:ai-egress-jp",
            domains=["chatgpt.com", "*.chatgpt.com"],
            tag_owner="autogroup:admin",
            member_src="autogroup:member",
        )
        self.assertEqual(merged["tagOwners"]["tag:ai-egress-jp"], ["autogroup:admin"])
        self.assertEqual(merged["autoApprovers"]["routes"]["0.0.0.0/0"], ["tag:ai-egress-jp"])
        self.assertEqual(merged["autoApprovers"]["routes"]["::/0"], ["tag:ai-egress-jp"])
        self.assertEqual(len(merged["grants"]), 2)
        connector = merged["nodeAttrs"][0]["app"][tool.APP_CONNECTORS_KEY][0]
        self.assertEqual(connector["name"], "AI-Egress-JP")
        self.assertEqual(connector["connectors"], ["tag:ai-egress-jp"])
        self.assertEqual(connector["domains"], ["chatgpt.com", "*.chatgpt.com"])

    def test_restore_policy_rejects_invalid_backup_before_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "bad.hujson"
            backup.write_text("{ this is not valid policy", encoding="utf-8")
            args = argparse.Namespace(
                input=str(backup),
                tailnet="-",
                api_key=None,
                oauth_client_id=None,
                oauth_client_secret=None,
                oauth_scopes=None,
                prompt_token=False,
                backup_dir=tmp,
                dry_run=True,
            )
            with self.assertRaisesRegex(tool.PolicyError, "Could not parse policy"):
                tool.restore_policy(args)

    def test_restore_policy_rejects_non_object_backup_before_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "array.hujson"
            backup.write_text("[]", encoding="utf-8")
            args = argparse.Namespace(
                input=str(backup),
                tailnet="-",
                api_key=None,
                oauth_client_id=None,
                oauth_client_secret=None,
                oauth_scopes=None,
                prompt_token=False,
                backup_dir=tmp,
                dry_run=True,
            )
            with self.assertRaisesRegex(tool.PolicyError, "Tailnet policy must be a JSON object"):
                tool.restore_policy(args)

    def test_grant_key_ignores_comment(self):
        grant_a = {"src": ["autogroup:member"], "dst": ["autogroup:internet"], "ip": ["*"], "comment": "ok"}
        grant_b = {"src": ["autogroup:member"], "dst": ["autogroup:internet"], "ip": ["*"]}
        self.assertEqual(tool.grant_key(grant_a), tool.grant_key(grant_b))

    def test_api_timeout_reads_environment(self):
        with mock.patch.dict(os.environ, {"TAILSCALE_API_TIMEOUT": "12.5"}):
            self.assertEqual(tool.api_timeout(), 12.5)

        with mock.patch.dict(os.environ, {"TAILSCALE_API_TIMEOUT": "0"}):
            with self.assertRaisesRegex(tool.PolicyError, "positive number"):
                tool.api_timeout()

    def test_http_request_retries_transient_http_error_once(self):
        class FakeResponse:
            status = 200
            headers = {"ETag": "abc"}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b"ok"

        transient = urllib.error.HTTPError(
            "https://api.example.test",
            503,
            "Service Unavailable",
            {},
            io.BytesIO(b"busy"),
        )
        with mock.patch.object(tool.urllib.request, "urlopen", side_effect=[transient, FakeResponse()]) as urlopen:
            with mock.patch.object(tool.time, "sleep") as sleep:
                status, text, headers = tool.http_request("GET", "https://api.example.test")

        self.assertEqual(status, 200)
        self.assertEqual(text, "ok")
        self.assertEqual(headers["ETag"], "abc")
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_http_request_does_not_retry_dns_failure(self):
        dns_error = urllib.error.URLError(socket.gaierror("temporary name resolution failure"))
        with mock.patch.object(tool.urllib.request, "urlopen", side_effect=dns_error) as urlopen:
            with mock.patch.object(tool.time, "sleep") as sleep:
                with self.assertRaisesRegex(tool.PolicyError, "Network request failed"):
                    tool.http_request("GET", "https://api.example.test")

        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    def test_get_oauth_token_uses_client_credentials_flow(self):
        with mock.patch.object(
            tool,
            "http_request",
            return_value=(200, '{"access_token":"oauth-token"}', {}),
        ) as request:
            token = tool.get_oauth_token("client-id", "client-secret", "policy_file")

        self.assertEqual(token, "oauth-token")
        _method, _url = request.call_args.args
        body = request.call_args.kwargs["body"]
        self.assertIn("grant_type=client_credentials", body)
        self.assertIn("client_id=client-id", body)
        self.assertIn("client_secret=client-secret", body)
        self.assertEqual(request.call_args.kwargs["content_type"], "application/x-www-form-urlencoded")

    def test_get_oauth_token_rejects_empty_access_token(self):
        with mock.patch.object(
            tool,
            "http_request",
            return_value=(200, '{"access_token":""}', {}),
        ):
            with self.assertRaisesRegex(tool.PolicyError, "access_token"):
                tool.get_oauth_token("client-id", "client-secret", "policy_file")

    def test_validate_input_reports_policy_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = Path(tmp) / "policy.hujson"
            policy.write_text('{"grants": {}}', encoding="utf-8")
            result = self.run_policy_tool("validate", "--input", str(policy), "--report", "json")

        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["tool"], "policy_tool.py")
        self.assertEqual(payload["command"], "validate")
        self.assertEqual(payload["summary"]["fail"], 1)
        self.assertTrue(any(item["id"] == "grants-shape" for item in payload["findings"]))

    def test_validate_reports_malformed_hujson(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = Path(tmp) / "policy.hujson"
            policy.write_text("{ this is not valid policy", encoding="utf-8")
            result = self.run_policy_tool("validate", "--input", str(policy), "--report", "json")

        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["findings"][0]["id"], "policy-parse")
        self.assertEqual(payload["findings"][0]["status"], "fail")

    def test_validate_domain_config_warns_on_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            domains = Path(tmp) / "domains.txt"
            domains.write_text("ChatGPT.com\nchatgpt.com.\n", encoding="utf-8")
            result = self.run_policy_tool("validate", "--domains-file", str(domains), "--report", "json")

        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["summary"]["warn"], 1)
        self.assertTrue(any(item["id"] == "duplicate-domains" for item in payload["findings"]))

    def test_validate_warns_on_broad_cdn_domain(self):
        # A CDN domain warns but validate still exits 0 (warnings don't fail the config).
        with tempfile.TemporaryDirectory() as tmp:
            domains = Path(tmp) / "domains.txt"
            domains.write_text("chatgpt.com\n*.cloudfront.net\n", encoding="utf-8")
            result = self.run_policy_tool("validate", "--domains-file", str(domains), "--report", "json")
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertGreaterEqual(payload["summary"]["warn"], 1)
        self.assertTrue(any(item["id"] == "broad-wildcard-warning" for item in payload["findings"]))

    def test_validate_fails_on_blocked_wildcard(self):
        # A blocked broad wildcard still FAILS validate (exit 1) -- warn-list did not soften it.
        with tempfile.TemporaryDirectory() as tmp:
            domains = Path(tmp) / "domains.txt"
            domains.write_text("*.com\n", encoding="utf-8")
            result = self.run_policy_tool("validate", "--domains-file", str(domains), "--report", "json")
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertTrue(any(item["status"] == "fail" for item in payload["findings"]))

    def test_validate_combined_policy_and_connector_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = Path(tmp) / "policy.hujson"
            domains = Path(tmp) / "domains.txt"
            policy.write_text("{}\n", encoding="utf-8")
            domains.write_text("chatgpt.com\n", encoding="utf-8")
            result = self.run_policy_tool(
                "validate",
                "--input",
                str(policy),
                "--domains-file",
                str(domains),
                "--report",
                "json",
            )

        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        ids = {item["id"] for item in payload["findings"]}
        self.assertIn("policy-root", ids)
        self.assertIn("connector-name", ids)
        self.assertIn("domains", ids)

    def test_validate_rejects_diff_arg(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = Path(tmp) / "policy.hujson"
            policy.write_text("{}\n", encoding="utf-8")
            result = self.run_policy_tool("validate", "--input", str(policy), "--diff")

        self.assertEqual(result.returncode, 2)
        self.assertIn("unrecognized arguments: --diff", result.stderr)

    def test_diff_outputs_unified_policy_additions(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = Path(tmp) / "policy.hujson"
            domains = Path(tmp) / "domains.txt"
            policy.write_text("{}\n", encoding="utf-8")
            domains.write_text("chatgpt.com\n", encoding="utf-8")
            result = self.run_policy_tool("diff", "--input", str(policy), "--domains-file", str(domains))

        self.assertEqual(result.returncode, 0)
        self.assertIn("---", result.stdout)
        self.assertIn("+++", result.stdout)
        self.assertIn('+  "tagOwners": {', result.stdout)
        self.assertIn('+  "autoApprovers": {', result.stdout)
        self.assertIn('+  "grants": [', result.stdout)
        self.assertIn('+  "nodeAttrs": [', result.stdout)

    def test_merge_report_json_includes_idempotency(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = Path(tmp) / "policy.hujson"
            domains = Path(tmp) / "domains.txt"
            policy.write_text("{}\n", encoding="utf-8")
            domains.write_text("chatgpt.com\n", encoding="utf-8")
            result = self.run_policy_tool(
                "merge",
                "--input",
                str(policy),
                "--domains-file",
                str(domains),
                "--report",
                "json",
            )

        self.assertEqual(result.returncode, 0)
        self.assertIn('"tagOwners"', result.stdout)
        payload = json.loads(result.stderr)
        idempotency = [item for item in payload["findings"] if item["id"] == "merge-idempotent"]
        self.assertEqual(idempotency[0]["status"], "ok")
        self.assertIn("no semantic changes", idempotency[0]["message"])

    def test_merge_without_report_skips_idempotency_second_merge(self):
        with mock.patch.object(tool, "merge_policy", wraps=tool.merge_policy) as merge:
            merged = tool.merge_with_report(
                {},
                connector_name="AI-Egress-JP",
                connector_tag="tag:ai-egress-jp",
                domains=["chatgpt.com"],
                tag_owner="autogroup:admin",
                member_src="autogroup:member",
            )

        self.assertIn("tagOwners", merged)
        self.assertEqual(merge.call_count, 1)

    def test_snippet_command_outputs_policy_fragment(self):
        with tempfile.TemporaryDirectory() as tmp:
            domains = Path(tmp) / "domains.txt"
            domains.write_text("chatgpt.com\n", encoding="utf-8")
            result = self.run_policy_tool("snippet", "--domains-file", str(domains))

        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["tagOwners"]["tag:ai-egress-jp"], ["autogroup:admin"])
        self.assertEqual(payload["nodeAttrs"][0]["app"][tool.APP_CONNECTORS_KEY][0]["domains"], ["chatgpt.com"])

    def test_apply_command_is_removed_with_pointer(self):
        # The direct 'apply' command is a migration tombstone: any historical
        # invocation exits 1 and points at plan / apply-plan.
        invocations = (
            ["apply"],
            [
                "apply", "--tailnet", "-",
                "--domains-file", str(ROOT / "policy/default-ai-domains.json"),
                "--dry-run", "--diff",
            ],
            # --diff WITHOUT --dry-run proves the obsolete --diff/--report guard is
            # gone: it must reach the removal pointer, not the guard's error.
            ["apply", "--diff", "--tailnet", "-"],
        )
        for argv in invocations:
            with self.subTest(argv=argv):
                result = self.run_policy_tool(*argv)
                self.assertEqual(result.returncode, 1, result.stderr)
                # Assert the full, unique tombstone message (not just fragments) so
                # only dispatch reaching apply_removed can satisfy it — a differently
                # worded guard sharing "'plan'"/"'apply-plan'" would not.
                self.assertIn("'apply' has been removed.", result.stderr)
                self.assertIn("with 'plan'", result.stderr)
                self.assertIn("'apply-plan <plan-dir>'", result.stderr)
                self.assertNotIn("only supported with apply --dry-run", result.stderr)

    def test_apply_help_shows_removal_pointer(self):
        # `apply --help` is handled by argparse before dispatch, so the pointer
        # must live in the subparser description (not just the handler)...
        sub = self.run_policy_tool("apply", "--help")
        self.assertEqual(sub.returncode, 0)
        self.assertIn("'apply' has been removed.", sub.stdout)
        self.assertIn("'apply-plan <plan-dir>'", sub.stdout)
        # ...and the top-level command listing (what people scan) must flag it too.
        root = self.run_policy_tool("--help")
        self.assertEqual(root.returncode, 0)
        self.assertIn("Removed", root.stdout)
        self.assertIn("apply-plan", root.stdout)

    def _no_cred_env(self):
        # Neutralize any host credential env vars so token tests are deterministic.
        return mock.patch.dict(
            os.environ,
            {
                "TAILSCALE_API_KEY": "",
                "TAILSCALE_OAUTH_CLIENT_ID": "",
                "TAILSCALE_OAUTH_CLIENT_SECRET": "",
                "TAILSCALE_API_AUTH": "bearer",
            },
        )

    def test_get_api_token_falls_back_to_oauth_when_no_api_key(self):
        args = argparse.Namespace(
            api_key=None, oauth_client_id="cid", oauth_client_secret="csec",
            oauth_scopes=None, prompt_token=False,
        )
        with self._no_cred_env():
            with mock.patch.object(tool, "get_oauth_token", return_value="oauth-tok") as goto:
                token, mode = tool.get_api_token(args)
        self.assertEqual((token, mode), ("oauth-tok", "bearer"))
        goto.assert_called_once_with("cid", "csec", mock.ANY)

    def test_get_api_token_missing_credential_raises(self):
        args = argparse.Namespace(
            api_key=None, oauth_client_id=None, oauth_client_secret=None,
            oauth_scopes=None, prompt_token=False,
        )
        with self._no_cred_env():
            with self.assertRaisesRegex(tool.PolicyError, "Missing credential"):
                tool.get_api_token(args)

    def test_tailscale_api_bearer_falls_back_to_basic_on_401(self):
        with mock.patch.object(
            tool,
            "http_request",
            side_effect=[
                (401, "unauthorized", {}),
                (200, "ok", {"ETag": "x"}),
            ],
        ) as hr:
            status, text, used_mode, headers = tool.tailscale_api(
                "POST", "/tailnet/-/acl", token="tskey-api-x", token_mode="bearer", body="{}",
            )
        self.assertEqual(status, 200)
        self.assertEqual(used_mode, "basic")
        self.assertEqual(hr.call_count, 2)
        self.assertEqual(hr.call_args_list[0].kwargs["token_mode"], "bearer")
        self.assertEqual(hr.call_args_list[1].kwargs["token_mode"], "basic")

    def test_restore_policy_dry_run_validates_and_prints_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "backup.hujson"
            backup.write_text('{"grants":[]}\n', encoding="utf-8")
            args = argparse.Namespace(
                input=str(backup),
                tailnet="-",
                api_key="tskey-api-test",
                oauth_client_id=None,
                oauth_client_secret=None,
                oauth_scopes=None,
                prompt_token=False,
                backup_dir=tmp,
                dry_run=True,
            )
            with mock.patch.object(
                tool,
                "tailscale_api",
                side_effect=[
                    (200, "{}\n", "bearer", {"ETag": "abc"}),
                    (200, "ok", "bearer", {}),
                ],
            ):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with mock.patch.object(sys, "stdout", stdout):
                    with mock.patch.object(sys, "stderr", stderr):
                        tool.restore_policy(args)

        self.assertEqual(stdout.getvalue(), '{"grants":[]}\n')

    def test_legacy_restore_uses_mixed_case_etag_for_if_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "backup.hujson"
            backup.write_text('{"grants":[]}\n', encoding="utf-8")
            args = argparse.Namespace(
                input=str(backup),
                tailnet="-",
                api_key="tskey-api-test",
                oauth_client_id=None,
                oauth_client_secret=None,
                oauth_scopes=None,
                prompt_token=False,
                backup_dir=tmp,
                dry_run=False,
            )
            with mock.patch.object(
                tool,
                "tailscale_api",
                side_effect=[
                    (200, "{}\n", "bearer", {"Etag": "restore-etag"}),
                    (200, "ok", "bearer", {}),
                    (200, "restored", "bearer", {}),
                ],
            ) as api:
                tool.restore_policy(args)

        self.assertEqual(api.call_args_list[2].kwargs["extra_headers"], {"If-Match": "restore-etag"})

    def make_plan_args(self, tmp, domains_file):
        return argparse.Namespace(
            domains_file=str(domains_file),
            connector_name="AI-Egress-JP",
            connector_tag="tag:ai-egress-jp",
            tag_owner="autogroup:admin",
            member_src="autogroup:member",
            allow_broad_wildcard=False,
            tailnet="-",
            api_key="tskey-api-test",
            oauth_client_id=None,
            oauth_client_secret=None,
            oauth_scopes=None,
            prompt_token=False,
            plans_dir=str(Path(tmp) / "policy-plans"),
        )

    def write_plan_bundle(
        self,
        tmp,
        *,
        status="valid",
        schema_version=None,
        etag="planning-etag",
        plan_id="20260527T143022Z-a1b2c3d4",
    ):
        plan_dir = Path(tmp) / f"plan.{plan_id}"
        plan_dir.mkdir()
        current_text = '{"grants":[]}\n'
        merged_text = '{"grants":[],"tagOwners":{"tag:ai-egress-jp":["autogroup:admin"]}}\n'
        (plan_dir / "current.hujson").write_text(current_text, encoding="utf-8")
        (plan_dir / "merged.json").write_text(merged_text, encoding="utf-8")
        (plan_dir / "diff.patch").write_text("--- current\n+++ merged\n", encoding="utf-8")
        manifest = {
            "schema_version": tool.MANIFEST_SCHEMA_VERSION if schema_version is None else schema_version,
            "tool_version": tool.__version__,
            "plan_id": plan_id,
            "status": status,
            "created_at": "2026-05-27T14:30:22Z",
            "tailnet": "-",
            "connector_name": "AI-Egress-JP",
            "connector_tag": "tag:ai-egress-jp",
            "domains_sha256": tool.sha256_text('["chatgpt.com"]\n'),
            "current_sha256": tool.sha256_text(current_text),
            "merged_sha256": tool.sha256_text(merged_text),
            "etag": etag,
            "summary": {"ok": 1, "warn": 0, "fail": 0},
            "findings": [tool.finding("ok", "test", "ok")],
        }
        if status in {"applied", "restored"}:
            manifest["applied_at"] = "2026-05-27T15:00:00Z"
        if status == "restored":
            manifest["restored_at"] = "2026-05-27T15:30:00Z"
        (plan_dir / "manifest.json").write_text(tool.dumps(manifest), encoding="utf-8")
        return plan_dir, manifest, current_text, merged_text

    def apply_plan_args(self, plan_dir, *, yes=True):
        return argparse.Namespace(
            plan_dir=str(plan_dir),
            tailnet=None,
            api_key="tskey-api-test",
            oauth_client_id=None,
            oauth_client_secret=None,
            oauth_scopes=None,
            prompt_token=False,
            yes=yes,
        )

    def test_plan_creates_valid_bundle_with_manifest_and_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            domains = Path(tmp) / "domains.txt"
            domains.write_text("chatgpt.com\n", encoding="utf-8")
            args = self.make_plan_args(tmp, domains)
            with mock.patch.object(
                tool,
                "tailscale_api",
                side_effect=[
                    (200, "{}\n", "bearer", {"ETag": "planning-etag"}),
                    (200, "ok", "bearer", {}),
                    (200, '{"preview":true}\n', "bearer", {}),
                ],
            ):
                stdout = io.StringIO()
                with mock.patch.object(sys, "stdout", stdout):
                    rc = tool.create_policy_plan(args)

            self.assertEqual(rc, 0)
            plans = list((Path(tmp) / "policy-plans").glob("plan.*"))
            self.assertEqual(len(plans), 1)
            plan_dir = plans[0]
            self.assertTrue((plan_dir / "current.hujson").exists())
            self.assertTrue((plan_dir / "merged.json").exists())
            self.assertTrue((plan_dir / "diff.patch").exists())
            self.assertTrue((plan_dir / "api-preview.json").exists())
            manifest = json.loads((plan_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["tool_version"], tool.__version__)
            self.assertEqual(manifest["status"], "valid")
            self.assertEqual(manifest["etag"], "planning-etag")
            self.assertEqual(manifest["merged_sha256"], tool.sha256_text((plan_dir / "merged.json").read_text(encoding="utf-8")))
            self.assertIn("Plan directory:", stdout.getvalue())
            self.assertIn("Diff summary:", stdout.getvalue())

    def test_plan_preview_failure_is_warning_without_preview_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            domains = Path(tmp) / "domains.txt"
            domains.write_text("chatgpt.com\n", encoding="utf-8")
            args = self.make_plan_args(tmp, domains)
            with mock.patch.object(
                tool,
                "tailscale_api",
                side_effect=[
                    (200, "{}\n", "bearer", {"ETag": "planning-etag"}),
                    (200, "ok", "bearer", {}),
                    (500, '{"error":"preview down"}', "bearer", {}),
                ],
            ):
                stdout = io.StringIO()
                with mock.patch.object(sys, "stdout", stdout):
                    rc = tool.create_policy_plan(args)

            self.assertEqual(rc, 0)
            plans = list((Path(tmp) / "policy-plans").glob("plan.*"))
            self.assertEqual(len(plans), 1)
            self.assertFalse((plans[0] / "api-preview.json").exists())
            manifest = json.loads((plans[0] / "manifest.json").read_text(encoding="utf-8"))
            preview_findings = [item for item in manifest["findings"] if item["id"] == "tailscale-api-preview"]
            self.assertEqual(preview_findings[0]["status"], "warn")

    def test_plan_validation_failure_writes_failed_artifacts_without_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            domains = Path(tmp) / "domains.txt"
            domains.write_text("chatgpt.com\n", encoding="utf-8")
            args = self.make_plan_args(tmp, domains)
            with mock.patch.object(
                tool,
                "tailscale_api",
                side_effect=[
                    (200, "{}\n", "bearer", {"ETag": "planning-etag"}),
                    (400, '{"error":"invalid"}', "bearer", {}),
                ],
            ):
                stdout = io.StringIO()
                with mock.patch.object(sys, "stdout", stdout):
                    rc = tool.create_policy_plan(args)

            self.assertEqual(rc, 1)
            failed = list((Path(tmp) / "policy-plans").glob("failed.*"))
            self.assertEqual(len(failed), 1)
            self.assertTrue((failed[0] / "merged.json").exists())
            self.assertTrue((failed[0] / "diff.patch").exists())
            self.assertTrue((failed[0] / "report.invalid.json").exists())
            self.assertFalse((failed[0] / "manifest.json").exists())

    def test_plan_directory_write_cleans_temp_dir_on_mid_write_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            final_dir = Path(tmp) / "plan.20260527T143022Z-a1b2c3d4"
            with self.assertRaisesRegex(tool.PolicyError, "Invalid plan artifact path"):
                tool.write_plan_directory(final_dir, {"manifest.json": "{}", "nested/file": "bad"})

            self.assertFalse(final_dir.exists())
            self.assertEqual(list(Path(tmp).glob(".tmp.*")), [])

    def test_plan_directory_uses_os_replace_for_final_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            final_dir = Path(tmp) / "plan.20260527T143022Z-a1b2c3d4"
            with mock.patch.object(tool.os, "replace", wraps=os.replace) as replace:
                tool.write_plan_directory(final_dir, {"manifest.json": "{}\n"})

        replace.assert_called_once()

    def test_plan_directory_written_with_private_permissions(self):
        # Plan bundles carry the full tailnet policy; the directory must be 0700
        # and each artifact 0600, regardless of the process umask. Force a
        # permissive umask so a reverted (no-chmod) implementation would fail.
        old_umask = os.umask(0o022)
        self.addCleanup(os.umask, old_umask)
        with tempfile.TemporaryDirectory() as tmp:
            final_dir = Path(tmp) / "plan.20260527T143022Z-a1b2c3d4"
            tool.write_plan_directory(final_dir, {"manifest.json": "{}\n", "merged.json": "{}\n"})
            self.assertEqual(final_dir.stat().st_mode & 0o777, 0o700)
            for name in ("manifest.json", "merged.json"):
                self.assertEqual((final_dir / name).stat().st_mode & 0o777, 0o600, name)

    def test_backup_written_with_private_permissions(self):
        old_umask = os.umask(0o022)
        self.addCleanup(os.umask, old_umask)
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "generated"
            path = tool.write_backup(backup_dir, "{}\n")
            self.assertEqual(backup_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_manifest_status_rewrite_keeps_private_permissions(self):
        # A later status update rewrites manifest.json via write_text_atomic; the
        # atomic replace must not widen the manifest back to the process umask.
        old_umask = os.umask(0o022)
        self.addCleanup(os.umask, old_umask)
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan.20260527T143022Z-a1b2c3d4"
            tool.write_plan_directory(plan_dir, {"manifest.json": "{}\n"})
            tool.update_manifest_status(plan_dir, {"schema_version": 1}, "applied", "applied_at")
            self.assertEqual((plan_dir / "manifest.json").stat().st_mode & 0o777, 0o600)

    def test_valid_plan_artifacts_reports_unwritable_output_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(Path, "mkdir", side_effect=OSError("permission denied")):
                with self.assertRaisesRegex(tool.PolicyError, "Could not write plan bundle"):
                    tool.valid_plan_artifacts(
                        plans_dir=Path(tmp) / "policy-plans",
                        plan_id="20260527T143022Z-a1b2c3d4",
                        manifest={"schema_version": 1},
                        current_text="{}\n",
                        merged_text="{}\n",
                        diff_text="",
                        preview_text=None,
                    )

    def test_plan_ids_created_same_second_have_random_suffix(self):
        fixed = dt.datetime(2026, 5, 27, 14, 30, 22, tzinfo=dt.timezone.utc)
        with mock.patch.object(tool, "utc_now", return_value=fixed):
            with mock.patch.object(tool.secrets, "token_hex", side_effect=["a1b2c3d4", "d4c3b2a1"]):
                first = tool.new_plan_id()
                second = tool.new_plan_id()

        self.assertEqual(first, "20260527T143022Z-a1b2c3d4")
        self.assertEqual(second, "20260527T143022Z-d4c3b2a1")
        self.assertNotEqual(first, second)

    def test_apply_plan_rejects_merged_hash_mismatch_before_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, _manifest, _current_text, _merged_text = self.write_plan_bundle(tmp)
            (plan_dir / "merged.json").write_text('{"tampered":true}\n', encoding="utf-8")
            args = self.apply_plan_args(plan_dir)
            with mock.patch.dict(os.environ, {"POLICY_RISK_ACK": "1"}):
                with mock.patch.object(tool, "tailscale_api") as api:
                    with self.assertRaisesRegex(tool.PolicyError, "SHA-256"):
                        tool.apply_policy_plan(args)
            api.assert_not_called()

    def test_apply_plan_revalidates_uses_planning_etag_and_marks_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, manifest, _current_text, _merged_text = self.write_plan_bundle(tmp)
            args = self.apply_plan_args(plan_dir)
            with mock.patch.dict(os.environ, {"POLICY_RISK_ACK": "1"}):
                with mock.patch.object(
                    tool,
                    "tailscale_api",
                    side_effect=[
                        (200, "ok", "bearer", {}),
                        (200, "applied", "bearer", {}),
                    ],
                ) as api:
                    rc = tool.apply_policy_plan(args)

            self.assertEqual(rc, 0)
            apply_kwargs = api.call_args_list[1].kwargs
            self.assertEqual(apply_kwargs["extra_headers"], {"If-Match": "planning-etag"})
            updated = json.loads((plan_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(updated["status"], "applied")
            self.assertIn("applied_at", updated)
            self.assertEqual(updated["current_sha256"], manifest["current_sha256"])
            self.assertEqual(updated["merged_sha256"], manifest["merged_sha256"])

    def test_apply_plan_rejects_stale_planning_etag(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, _manifest, _current_text, _merged_text = self.write_plan_bundle(tmp)
            args = self.apply_plan_args(plan_dir)
            with mock.patch.dict(os.environ, {"POLICY_RISK_ACK": "1"}):
                with mock.patch.object(
                    tool,
                    "tailscale_api",
                    side_effect=[
                        (200, "ok", "bearer", {}),
                        (412, "stale", "bearer", {}),
                    ],
                ):
                    with self.assertRaisesRegex(tool.PolicyError, "Planning ETag is stale"):
                        tool.apply_policy_plan(args)
            manifest = json.loads((plan_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "valid")

    def test_apply_plan_rejects_revalidation_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, _manifest, _current_text, _merged_text = self.write_plan_bundle(tmp)
            args = self.apply_plan_args(plan_dir)
            with mock.patch.dict(os.environ, {"POLICY_RISK_ACK": "1"}):
                with mock.patch.object(
                    tool,
                    "tailscale_api",
                    return_value=(400, '{"error":"invalid"}', "bearer", {}),
                ):
                    with self.assertRaisesRegex(tool.PolicyError, "Plan revalidation failed"):
                        tool.apply_policy_plan(args)

            manifest = json.loads((plan_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "valid")

    def test_apply_plan_validation_discovers_basic_auth_fallback_for_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, _manifest, _current_text, _merged_text = self.write_plan_bundle(tmp)
            args = self.apply_plan_args(plan_dir)
            with mock.patch.dict(os.environ, {"POLICY_RISK_ACK": "1"}):
                with mock.patch.object(
                    tool,
                    "tailscale_api",
                    side_effect=[
                        (200, "ok", "basic", {}),
                        (200, "applied", "basic", {}),
                    ],
                ) as api:
                    tool.apply_policy_plan(args)

            self.assertIs(api.call_args_list[0].kwargs["allow_basic_fallback"], True)
            self.assertEqual(api.call_args_list[1].kwargs["token_mode"], "basic")

    def test_apply_plan_reports_when_apply_succeeds_but_manifest_update_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, _manifest, _current_text, _merged_text = self.write_plan_bundle(tmp)
            args = self.apply_plan_args(plan_dir)
            with mock.patch.dict(os.environ, {"POLICY_RISK_ACK": "1"}):
                with mock.patch.object(
                    tool,
                    "tailscale_api",
                    side_effect=[
                        (200, "ok", "bearer", {}),
                        (200, "applied", "bearer", {}),
                    ],
                ):
                    with mock.patch.object(tool, "update_manifest_status", side_effect=tool.PolicyError("disk full")):
                        with self.assertRaisesRegex(tool.PolicyError, "was applied, but manifest.json could not be updated"):
                            tool.apply_policy_plan(args)

    def test_apply_plan_rejects_wrong_interactive_confirmation_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, _manifest, _current_text, _merged_text = self.write_plan_bundle(tmp)
            args = self.apply_plan_args(plan_dir, yes=False)
            with mock.patch.object(sys.stdin, "isatty", return_value=True):
                with mock.patch("builtins.input", return_value="APPLY wrong-plan"):
                    with self.assertRaisesRegex(tool.PolicyError, "expected exactly"):
                        tool.apply_policy_plan(args)

    def test_restore_plan_fetches_fresh_etag_and_marks_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, manifest, _current_text, _merged_text = self.write_plan_bundle(tmp, status="applied")
            args = self.apply_plan_args(plan_dir)
            with mock.patch.dict(os.environ, {"POLICY_RISK_ACK": "1"}):
                with mock.patch.object(
                    tool,
                    "tailscale_api",
                    side_effect=[
                        (200, '{"current":"latest"}\n', "bearer", {"ETag": "fresh-etag"}),
                        (200, "ok", "bearer", {}),
                        (200, "restored", "bearer", {}),
                    ],
                ) as api:
                    rc = tool.restore_policy_plan(args)

            self.assertEqual(rc, 0)
            self.assertEqual(api.call_args_list[2].kwargs["extra_headers"], {"If-Match": "fresh-etag"})
            updated = json.loads((plan_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(updated["status"], "restored")
            self.assertEqual(updated["current_sha256"], manifest["current_sha256"])
            self.assertEqual(updated["merged_sha256"], manifest["merged_sha256"])

    def test_restore_plan_allows_repeated_restore_and_appends_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, manifest, _current_text, _merged_text = self.write_plan_bundle(tmp, status="restored")
            args = self.apply_plan_args(plan_dir)
            with mock.patch.dict(os.environ, {"POLICY_RISK_ACK": "1"}):
                with mock.patch.object(
                    tool,
                    "tailscale_api",
                    side_effect=[
                        (200, '{"current":"latest"}\n', "bearer", {"ETag": "fresh-etag"}),
                        (200, "ok", "bearer", {}),
                        (200, "restored", "bearer", {}),
                    ],
                ):
                    rc = tool.restore_policy_plan(args)

            self.assertEqual(rc, 0)
            updated = json.loads((plan_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(updated["status"], "restored")
            self.assertEqual(updated["restored_at"], manifest["restored_at"])
            self.assertEqual(len(updated["restored_at_history"]), 1)

    def test_restore_plan_rejects_current_hash_mismatch_before_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, _manifest, _current_text, _merged_text = self.write_plan_bundle(tmp, status="applied")
            (plan_dir / "current.hujson").write_text('{"tampered":true}\n', encoding="utf-8")
            args = self.apply_plan_args(plan_dir)
            with mock.patch.dict(os.environ, {"POLICY_RISK_ACK": "1"}):
                with mock.patch.object(tool, "tailscale_api") as api:
                    with self.assertRaisesRegex(tool.PolicyError, "current.hujson SHA-256"):
                        tool.restore_policy_plan(args)
            api.assert_not_called()

    def test_restore_plan_rejects_invalid_current_policy_before_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, manifest, _current_text, _merged_text = self.write_plan_bundle(tmp, status="applied")
            bad_text = "{ this is not policy"
            (plan_dir / "current.hujson").write_text(bad_text, encoding="utf-8")
            manifest["current_sha256"] = tool.sha256_text(bad_text)
            (plan_dir / "manifest.json").write_text(tool.dumps(manifest), encoding="utf-8")
            args = self.apply_plan_args(plan_dir)
            with mock.patch.dict(os.environ, {"POLICY_RISK_ACK": "1"}):
                with mock.patch.object(tool, "tailscale_api") as api:
                    with self.assertRaisesRegex(tool.PolicyError, "Could not parse policy"):
                        tool.restore_policy_plan(args)
            api.assert_not_called()

    def test_restore_plan_rejects_stale_fresh_etag(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, _manifest, _current_text, _merged_text = self.write_plan_bundle(tmp, status="applied")
            args = self.apply_plan_args(plan_dir)
            with mock.patch.dict(os.environ, {"POLICY_RISK_ACK": "1"}):
                with mock.patch.object(
                    tool,
                    "tailscale_api",
                    side_effect=[
                        (200, '{"current":"latest"}\n', "bearer", {"ETag": "fresh-etag"}),
                        (200, "ok", "bearer", {}),
                        (412, "stale", "bearer", {}),
                    ],
                ):
                    with self.assertRaisesRegex(tool.PolicyError, "fresh policy ETag"):
                        tool.restore_policy_plan(args)

            manifest = json.loads((plan_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "applied")

    def test_restore_plan_requires_noninteractive_ack_and_yes(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, _manifest, _current_text, _merged_text = self.write_plan_bundle(tmp, status="applied")
            args = self.apply_plan_args(plan_dir, yes=False)
            with mock.patch.object(sys.stdin, "isatty", return_value=False):
                with self.assertRaisesRegex(tool.PolicyError, "Non-interactive use requires --yes"):
                    tool.restore_policy_plan(args)

    def test_restore_plan_rejects_status_that_was_never_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, _manifest, _current_text, _merged_text = self.write_plan_bundle(tmp, status="valid")
            args = self.apply_plan_args(plan_dir)
            with self.assertRaisesRegex(tool.PolicyError, "status 'applied' or 'restored'"):
                tool.restore_policy_plan(args)

    def test_apply_plan_requires_noninteractive_ack_and_yes(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, _manifest, _current_text, _merged_text = self.write_plan_bundle(tmp)
            args = self.apply_plan_args(plan_dir, yes=False)
            with mock.patch.object(sys.stdin, "isatty", return_value=False):
                with self.assertRaisesRegex(tool.PolicyError, "Non-interactive use requires --yes"):
                    tool.apply_policy_plan(args)

    def test_manifest_unsupported_major_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, _manifest, _current_text, _merged_text = self.write_plan_bundle(tmp, schema_version="2.0")
            args = self.apply_plan_args(plan_dir)
            with self.assertRaisesRegex(tool.PolicyError, "Unsupported plan manifest"):
                tool.apply_policy_plan(args)

    def test_list_plans_reports_valid_applied_restored_and_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            plans_dir = Path(tmp) / "policy-plans"
            plans_dir.mkdir()
            for status, suffix in [
                ("valid", "a1b2c3d4"),
                ("applied", "b1b2c3d4"),
                ("restored", "c1b2c3d4"),
            ]:
                self.write_plan_bundle(
                    plans_dir,
                    status=status,
                    plan_id=f"20260527T143022Z-{suffix}",
                )
            failed = plans_dir / "failed.20260527T143022Z-d1b2c3d4"
            failed.mkdir()
            (failed / "report.invalid.json").write_text(tool.dumps({"summary": {"fail": 1}}), encoding="utf-8")

            args = argparse.Namespace(plans_dir=str(plans_dir), json=False)
            stdout = io.StringIO()
            with mock.patch.object(sys, "stdout", stdout):
                rc = tool.list_policy_plans(args)

        self.assertEqual(rc, 0)
        output = stdout.getvalue()
        for status in ("valid", "applied", "restored", "failed"):
            self.assertIn(status, output)

    def test_report_json_failure_schema_for_invalid_domain(self):
        with tempfile.TemporaryDirectory() as tmp:
            domains = Path(tmp) / "domains.txt"
            domains.write_text("https://example.com\n", encoding="utf-8")
            result = self.run_policy_tool("validate", "--domains-file", str(domains), "--report", "json")

        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(set(payload), {"schema_version", "tool", "command", "summary", "findings"})
        self.assertEqual(payload["summary"]["fail"], 1)
        self.assertEqual(payload["findings"][-1]["status"], "fail")

    def test_http_request_does_not_retry_ssl_url_error(self):
        error = urllib.error.URLError(ssl.SSLError("certificate verify failed"))
        with mock.patch.object(tool.urllib.request, "urlopen", side_effect=error) as urlopen:
            with mock.patch.object(tool.time, "sleep") as sleep:
                with self.assertRaises(tool.PolicyError):
                    tool.http_request("GET", "https://api.example.test")

        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()


READY_POLICY = {
    "tagOwners": {"tag:ai-egress-jp": ["autogroup:admin"], "tag:ai-egress-jp2": ["autogroup:admin"]},
    "autoApprovers": {
        "routes": {
            "0.0.0.0/0": ["tag:ai-egress-jp", "tag:ai-egress-jp2"],
            "::/0": ["tag:ai-egress-jp", "tag:ai-egress-jp2"],
        }
    },
    "grants": [
        {"src": ["autogroup:member"], "dst": ["tag:ai-egress-jp"], "ip": ["tcp:53", "udp:53"]},
        {"src": ["autogroup:member"], "dst": ["tag:ai-egress-jp2"], "ip": ["tcp:53", "udp:53"]},
    ],
    "nodeAttrs": [
        {
            "target": ["*"],
            "app": {
                tool.APP_CONNECTORS_KEY: [
                    {"name": "AI-Egress-JP", "connectors": ["tag:ai-egress-jp"], "domains": ["chatgpt.com"]}
                ]
            },
        }
    ],
}


class ConnectorPlanUnitTests(unittest.TestCase):
    def test_cas_equal_type_strict(self):
        self.assertFalse(tool.cas_equal([True], [1]))
        self.assertTrue(tool.cas_equal([True], [True]))
        self.assertFalse(tool.cas_equal("TAG:X", "tag:x"))
        self.assertFalse(tool.cas_equal(["a", "b"], ["b", "a"]))
        self.assertFalse(tool.cas_equal(["a", "a"], ["a"]))
        self.assertFalse(tool.cas_equal("tag:x", ["tag:x"]))
        self.assertFalse(tool.cas_equal([], None))
        self.assertTrue(tool.cas_equal(None, None))
        self.assertTrue(tool.cas_equal(["tag:x"], ["tag:x"]))

    def test_find_managed_connector_coords_counts_and_defensive(self):
        self.assertEqual(tool.find_managed_connector_coords(READY_POLICY, "AI-Egress-JP"), [(0, 0)])
        self.assertEqual(tool.find_managed_connector_coords(READY_POLICY, "Nope"), [])
        # Defensive: malformed shapes are skipped, never raised on.
        malformed = {"nodeAttrs": ["x", {"app": "notdict"}, {"app": {tool.APP_CONNECTORS_KEY: "notlist"}}]}
        self.assertEqual(tool.find_managed_connector_coords(malformed, "AI-Egress-JP"), [])
        self.assertEqual(tool.find_managed_connector_coords({"nodeAttrs": "notlist"}, "AI-Egress-JP"), [])

    def test_pool_readiness_containment_and_misses(self):
        self.assertIsNone(tool.pool_readiness_error(READY_POLICY, "tag:ai-egress-jp2", "autogroup:member"))
        # Undeclared tag.
        self.assertIsNotNone(tool.pool_readiness_error(READY_POLICY, "tag:ai-egress-xx", "autogroup:member"))
        # Route among others still satisfies (containment, not equality).
        among = copy.deepcopy(READY_POLICY)
        among["autoApprovers"]["routes"]["0.0.0.0/0"] = ["tag:other", "tag:ai-egress-jp2", "tag:more"]
        self.assertIsNone(tool.pool_readiness_error(among, "tag:ai-egress-jp2", "autogroup:member"))
        # DNS grant with extra ip entries still satisfies.
        rich = copy.deepcopy(READY_POLICY)
        rich["grants"][1]["ip"] = ["tcp:53", "udp:53", "tcp:443"]
        self.assertIsNone(tool.pool_readiness_error(rich, "tag:ai-egress-jp2", "autogroup:member"))

    def test_pool_readiness_inner_value_wrong_types_are_misses_not_crashes(self):
        scalar_owner = copy.deepcopy(READY_POLICY)
        scalar_owner["tagOwners"]["tag:ai-egress-jp2"] = "autogroup:admin"
        self.assertIsNotNone(tool.pool_readiness_error(scalar_owner, "tag:ai-egress-jp2", "autogroup:member"))
        scalar_route = copy.deepcopy(READY_POLICY)
        scalar_route["autoApprovers"]["routes"]["0.0.0.0/0"] = "tag:ai-egress-jp2"
        self.assertIsNotNone(tool.pool_readiness_error(scalar_route, "tag:ai-egress-jp2", "autogroup:member"))
        scalar_ip = copy.deepcopy(READY_POLICY)
        scalar_ip["grants"][1]["ip"] = "tcp:53"
        self.assertIsNotNone(tool.pool_readiness_error(scalar_ip, "tag:ai-egress-jp2", "autogroup:member"))

    def test_pool_readiness_parent_absent_is_miss(self):
        self.assertIsNotNone(tool.pool_readiness_error({"nodeAttrs": []}, "tag:ai-egress-jp2", "autogroup:member"))
        owners_only = {"tagOwners": {"tag:ai-egress-jp2": ["autogroup:admin"]}}
        self.assertIsNotNone(tool.pool_readiness_error(owners_only, "tag:ai-egress-jp2", "autogroup:member"))


class ConnectorPlanTests(unittest.TestCase):
    def cplan_args(self, tmp, *, switch_to=None, declare=None, connector_name="AI-Egress-JP",
                   expected_from=tool._EXPECTED_FROM_UNSET, member_src="autogroup:member",
                   tag_owner="autogroup:admin", api_key="tskey-api-test"):
        return argparse.Namespace(
            switch_to=switch_to,
            declare=declare,
            connector_name=connector_name,
            tag_owner=tag_owner,
            member_src=member_src,
            expected_from=expected_from,
            tailnet="-",
            api_key=api_key,
            oauth_client_id=None,
            oauth_client_secret=None,
            oauth_scopes=None,
            prompt_token=False,
            plans_dir=str(Path(tmp) / "policy-plans"),
        )

    def run_cplan(self, args, *, current, etag="planning-etag", get_status=200):
        current_text = tool.dumps(current) if not isinstance(current, str) else current
        get_headers = {"ETag": etag} if etag is not None else {}
        with mock.patch.object(
            tool,
            "tailscale_api",
            side_effect=[
                (get_status, current_text, "bearer", get_headers),
                (200, "ok", "bearer", {}),
                (200, "{}\n", "bearer", {}),
            ],
        ):
            stdout = io.StringIO()
            with mock.patch.object(sys, "stdout", stdout):
                rc = tool.connector_plan(args)
        return rc, stdout.getvalue()

    def only_plan(self, tmp):
        plans = list((Path(tmp) / "policy-plans").glob("plan.*"))
        self.assertEqual(len(plans), 1, plans)
        return plans[0]

    def only_failed(self, tmp):
        failed = list((Path(tmp) / "policy-plans").glob("failed.*"))
        self.assertEqual(len(failed), 1, failed)
        return failed[0]

    def manifest_of(self, plan_dir):
        return json.loads((plan_dir / "manifest.json").read_text(encoding="utf-8"))

    def failed_finding_ids(self, failed_dir):
        report = json.loads((failed_dir / "report.invalid.json").read_text(encoding="utf-8"))
        return [f["id"] for f in report["findings"] if f["status"] == "fail"]

    # --- switch happy + manifest shape ---
    def test_switch_happy_replaces_connectors_and_records_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.cplan_args(tmp, switch_to="tag:ai-egress-jp2")
            rc, _ = self.run_cplan(args, current=READY_POLICY)
            self.assertEqual(rc, 0)
            plan_dir = self.only_plan(tmp)
            merged = json.loads((plan_dir / "merged.json").read_text(encoding="utf-8"))
            entry = merged["nodeAttrs"][0]["app"][tool.APP_CONNECTORS_KEY][0]
            self.assertEqual(entry["connectors"], ["tag:ai-egress-jp2"])
            self.assertEqual(entry["domains"], ["chatgpt.com"])  # untouched
            for name in ("current.hujson", "merged.json", "diff.patch", "manifest.json", "api-preview.json"):
                self.assertTrue((plan_dir / name).exists(), name)
            man = self.manifest_of(plan_dir)
            self.assertEqual(man["operation"], "switch-connectors")
            self.assertEqual(man["from"], ["tag:ai-egress-jp"])
            self.assertEqual(man["to"], "tag:ai-egress-jp2")
            self.assertEqual(man["connector_tag"], "tag:ai-egress-jp2")
            self.assertEqual(man["connector_name"], "AI-Egress-JP")
            self.assertEqual(man["etag"], "planning-etag")
            self.assertEqual([f for f in man["findings"] if f["status"] == "fail"], [])
            self.assertEqual(man["summary"]["fail"], 0)

    def test_switch_manifest_omits_domains_sha256(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.cplan_args(tmp, switch_to="tag:ai-egress-jp2")
            self.run_cplan(args, current=READY_POLICY)
            man = self.manifest_of(self.only_plan(tmp))
            self.assertNotIn("domains_sha256", man)

    def test_switch_manifest_key_set_pin(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.cplan_args(tmp, switch_to="tag:ai-egress-jp2")
            self.run_cplan(args, current=READY_POLICY)
            man = self.manifest_of(self.only_plan(tmp))
            expected = {
                "schema_version", "tool_version", "plan_id", "status", "created_at", "tailnet",
                "connector_tag", "current_sha256", "merged_sha256", "etag", "summary", "findings",
                "operation", "from", "to", "connector_name",
            }
            self.assertEqual(set(man.keys()), expected)
            self.assertEqual(man["schema_version"], 1)

    def test_switch_diff_touches_only_connectors(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.cplan_args(tmp, switch_to="tag:ai-egress-jp2")
            self.run_cplan(args, current=READY_POLICY)
            diff = (self.only_plan(tmp) / "diff.patch").read_text(encoding="utf-8")
            changed = [ln for ln in diff.splitlines() if ln[:1] in {"+", "-"} and not ln.startswith(("+++", "---"))]
            # EXACT changed lines: the one connectors element and nothing else.
            self.assertEqual(
                changed,
                ['-              "tag:ai-egress-jp"', '+              "tag:ai-egress-jp2"'],
            )

    # --- reconciliation shapes (raw drift recovery) ---
    def _switch_from(self, tmp, current_connectors_present, current_value):
        policy = copy.deepcopy(READY_POLICY)
        entry = policy["nodeAttrs"][0]["app"][tool.APP_CONNECTORS_KEY][0]
        if current_connectors_present:
            entry["connectors"] = current_value
        else:
            del entry["connectors"]
        args = self.cplan_args(tmp, switch_to="tag:ai-egress-jp2")
        rc, _ = self.run_cplan(args, current=policy)
        return rc

    def test_switch_reconciles_empty_multi_outofpair_string_absent(self):
        for present, value, label in [
            (True, [], "empty"),
            (True, ["tag:ai-egress-jp", "tag:ai-egress-jp2"], "multi"),
            (True, ["tag:something-else"], "out-of-pair"),
            (True, "tag:ai-egress-jp", "string"),
            (False, None, "absent"),
        ]:
            with tempfile.TemporaryDirectory() as tmp:
                rc = self._switch_from(tmp, present, value)
                self.assertEqual(rc, 0, label)
                man = self.manifest_of(self.only_plan(tmp))
                self.assertEqual(man["from"], value if present else None, label)
                merged = json.loads((self.only_plan(tmp) / "merged.json").read_text(encoding="utf-8"))
                self.assertEqual(
                    merged["nodeAttrs"][0]["app"][tool.APP_CONNECTORS_KEY][0]["connectors"],
                    ["tag:ai-egress-jp2"], label,
                )

    def test_switch_same_target_string_reconciles_not_already_active(self):
        # A string-shaped current equal to the target is NOT already-active;
        # the plan normalizes the shape to a one-element list.
        with tempfile.TemporaryDirectory() as tmp:
            policy = copy.deepcopy(READY_POLICY)
            policy["nodeAttrs"][0]["app"][tool.APP_CONNECTORS_KEY][0]["connectors"] = "tag:ai-egress-jp2"
            args = self.cplan_args(tmp, switch_to="tag:ai-egress-jp2")
            rc, _ = self.run_cplan(args, current=policy)
            self.assertEqual(rc, 0)
            man = self.manifest_of(self.only_plan(tmp))
            self.assertEqual(man["from"], "tag:ai-egress-jp2")
            merged = json.loads((self.only_plan(tmp) / "merged.json").read_text(encoding="utf-8"))
            self.assertEqual(
                merged["nodeAttrs"][0]["app"][tool.APP_CONNECTORS_KEY][0]["connectors"], ["tag:ai-egress-jp2"]
            )

    def test_switch_already_active_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.cplan_args(tmp, switch_to="tag:ai-egress-jp")
            rc, _ = self.run_cplan(args, current=READY_POLICY)
            self.assertEqual(rc, 1)
            self.assertIn("already-active", self.failed_finding_ids(self.only_failed(tmp)))

    # --- CAS ---
    def test_switch_cas_match_proceeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.cplan_args(tmp, switch_to="tag:ai-egress-jp2", expected_from='["tag:ai-egress-jp"]')
            rc, _ = self.run_cplan(args, current=READY_POLICY)
            self.assertEqual(rc, 0)

    def test_switch_cas_mismatches_refuse(self):
        # (label, current connectors value or ABSENT, --expected-from JSON)
        ABSENT = object()
        cases = [
            ("order", ["tag:ai-egress-jp", "tag:ai-egress-jp2"], '["tag:ai-egress-jp2","tag:ai-egress-jp"]'),
            ("string-vs-list", ["tag:ai-egress-jp"], '"tag:ai-egress-jp"'),
            ("empty-vs-absent", ABSENT, "[]"),
            ("case-only", ["tag:ai-egress-jp"], '["TAG:AI-EGRESS-JP"]'),
            ("dup-multiplicity", ["tag:ai-egress-jp"], '["tag:ai-egress-jp","tag:ai-egress-jp"]'),
        ]
        for label, current_value, expected in cases:
            with tempfile.TemporaryDirectory() as tmp:
                policy = copy.deepcopy(READY_POLICY)
                entry = policy["nodeAttrs"][0]["app"][tool.APP_CONNECTORS_KEY][0]
                if current_value is ABSENT:
                    del entry["connectors"]
                else:
                    entry["connectors"] = current_value
                args = self.cplan_args(tmp, switch_to="tag:ai-egress-jp2", expected_from=expected)
                rc, _ = self.run_cplan(args, current=policy)
                self.assertEqual(rc, 1, label)
                self.assertIn("expected-from-mismatch", self.failed_finding_ids(self.only_failed(tmp)), label)

    def test_switch_cas_null_matches_absent_connectors(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = copy.deepcopy(READY_POLICY)
            del policy["nodeAttrs"][0]["app"][tool.APP_CONNECTORS_KEY][0]["connectors"]
            args = self.cplan_args(tmp, switch_to="tag:ai-egress-jp2", expected_from="null")
            rc, _ = self.run_cplan(args, current=policy)
            self.assertEqual(rc, 0)
            self.assertEqual(self.manifest_of(self.only_plan(tmp))["from"], None)

    def test_switch_cas_null_vs_present_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.cplan_args(tmp, switch_to="tag:ai-egress-jp2", expected_from="null")
            rc, _ = self.run_cplan(args, current=READY_POLICY)
            self.assertEqual(rc, 1)
            self.assertIn("expected-from-mismatch", self.failed_finding_ids(self.only_failed(tmp)))

    def test_switch_expected_from_omitted_performs_no_cas(self):
        # A present current that would fail any CAS still proceeds when --expected-from is omitted.
        with tempfile.TemporaryDirectory() as tmp:
            args = self.cplan_args(tmp, switch_to="tag:ai-egress-jp2")
            rc, _ = self.run_cplan(args, current=READY_POLICY)
            self.assertEqual(rc, 0)

    def test_switch_expected_from_invalid_json_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.cplan_args(tmp, switch_to="tag:ai-egress-jp2", expected_from="{not json")
            # config-stage refusal: no API calls happen.
            stdout = io.StringIO()
            with mock.patch.object(tool, "tailscale_api") as api:
                with mock.patch.object(sys, "stdout", stdout):
                    rc = tool.connector_plan(args)
            self.assertEqual(rc, 1)
            api.assert_not_called()
            self.assertIn("expected-from-invalid", self.failed_finding_ids(self.only_failed(tmp)))

    # --- managed entry ---
    def test_switch_missing_managed_entry_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.cplan_args(tmp, switch_to="tag:ai-egress-jp2", connector_name="AI-Egress-NOPE")
            rc, _ = self.run_cplan(args, current=READY_POLICY)
            self.assertEqual(rc, 1)
            self.assertIn("managed-entry", self.failed_finding_ids(self.only_failed(tmp)))

    def test_switch_duplicate_managed_entry_refuses_same_and_cross_block(self):
        for spread in ("same-block", "cross-block"):
            with tempfile.TemporaryDirectory() as tmp:
                policy = copy.deepcopy(READY_POLICY)
                dup = {"name": "AI-Egress-JP", "connectors": ["tag:x"]}
                if spread == "same-block":
                    policy["nodeAttrs"][0]["app"][tool.APP_CONNECTORS_KEY].append(dup)
                else:
                    policy["nodeAttrs"].append({"target": ["*"], "app": {tool.APP_CONNECTORS_KEY: [dup]}})
                args = self.cplan_args(tmp, switch_to="tag:ai-egress-jp2")
                rc, _ = self.run_cplan(args, current=policy)
                self.assertEqual(rc, 1, spread)
                self.assertIn("managed-entry", self.failed_finding_ids(self.only_failed(tmp)), spread)

    # --- readiness ---
    def test_switch_readiness_single_misses_refuse(self):
        def strip(mut):
            p = copy.deepcopy(READY_POLICY)
            mut(p)
            return p
        def drop_both_routes(p):
            p["autoApprovers"]["routes"]["0.0.0.0/0"].remove("tag:ai-egress-jp2")
            p["autoApprovers"]["routes"]["::/0"].remove("tag:ai-egress-jp2")

        mutators = {
            "tagowners-absent": lambda p: p["tagOwners"].pop("tag:ai-egress-jp2"),
            "tagowners-empty": lambda p: p["tagOwners"].__setitem__("tag:ai-egress-jp2", []),
            "one-route-missing": lambda p: p["autoApprovers"]["routes"]["::/0"].remove("tag:ai-egress-jp2"),
            "both-routes-missing": drop_both_routes,
            "autoapprovers-parent-absent": lambda p: p.pop("autoApprovers"),
            "grants-parent-absent": lambda p: p.pop("grants"),
            "dns-grant-absent": lambda p: p["grants"].pop(1),
            "dns-missing-udp": lambda p: p["grants"][1].__setitem__("ip", ["tcp:53"]),
            "dns-scalar-src": lambda p: p["grants"][1].__setitem__("src", "autogroup:member"),
            "dns-scalar-dst": lambda p: p["grants"][1].__setitem__("dst", "tag:ai-egress-jp2"),
        }
        for label, mut in mutators.items():
            with tempfile.TemporaryDirectory() as tmp:
                args = self.cplan_args(tmp, switch_to="tag:ai-egress-jp2")
                rc, _ = self.run_cplan(args, current=strip(mut))
                self.assertEqual(rc, 1, label)
                self.assertIn("pool-readiness", self.failed_finding_ids(self.only_failed(tmp)), label)

    def test_switch_readiness_positive_route_among_others_and_any_owner(self):
        policy = copy.deepcopy(READY_POLICY)
        policy["tagOwners"]["tag:ai-egress-jp2"] = ["autogroup:someone-else"]
        policy["autoApprovers"]["routes"]["0.0.0.0/0"] = ["tag:x", "tag:ai-egress-jp2", "tag:y"]
        # DNS grant with superset src/dst/ip still satisfies containment.
        policy["grants"][1] = {
            "src": ["autogroup:member", "group:ops"],
            "dst": ["tag:ai-egress-jp2", "tag:other"],
            "ip": ["tcp:53", "udp:53", "tcp:443"],
        }
        with tempfile.TemporaryDirectory() as tmp:
            args = self.cplan_args(tmp, switch_to="tag:ai-egress-jp2")
            rc, _ = self.run_cplan(args, current=policy)
            self.assertEqual(rc, 0)

    # --- tripwire ---
    def test_switch_tripwire_extra_change_refuses(self):
        original = tool.mutate_switch

        def leaky(policy, coords, target):
            m = original(policy, coords, target)
            i, j = coords
            m["nodeAttrs"][i]["app"][tool.APP_CONNECTORS_KEY][j]["domains"] = ["evil.example"]
            return m

        with tempfile.TemporaryDirectory() as tmp:
            args = self.cplan_args(tmp, switch_to="tag:ai-egress-jp2")
            with mock.patch.object(tool, "mutate_switch", leaky):
                rc, _ = self.run_cplan(args, current=READY_POLICY)
            self.assertEqual(rc, 1)
            self.assertIn("connector-only-diff", self.failed_finding_ids(self.only_failed(tmp)))

    # --- API failure surface split ---
    def test_missing_credential_raises_no_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.cplan_args(tmp, switch_to="tag:ai-egress-jp2", api_key=None)
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(tool.PolicyError):
                    tool.connector_plan(args)
            self.assertEqual(list((Path(tmp) / "policy-plans").glob("*")), [])

    def test_get_failure_raises_no_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.cplan_args(tmp, switch_to="tag:ai-egress-jp2")
            with mock.patch.object(tool, "tailscale_api", side_effect=[(500, "boom", "bearer", {})]):
                with self.assertRaises(tool.PolicyError):
                    tool.connector_plan(args)
            self.assertEqual(list((Path(tmp) / "policy-plans").glob("*")), [])

    def test_missing_and_empty_etag_raise_no_artifacts(self):
        for etag in (None, ""):
            with tempfile.TemporaryDirectory() as tmp:
                args = self.cplan_args(tmp, switch_to="tag:ai-egress-jp2")
                headers = {} if etag is None else {"ETag": ""}
                with mock.patch.object(
                    tool, "tailscale_api",
                    side_effect=[(200, tool.dumps(READY_POLICY), "bearer", headers)],
                ):
                    with self.assertRaises(tool.PolicyError) as ctx:
                        tool.connector_plan(args)
                self.assertIn("planning-etag-missing", str(ctx.exception))
                self.assertEqual(list((Path(tmp) / "policy-plans").glob("*")), [])

    def test_shape_invalid_fetched_policy_writes_failed_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.cplan_args(tmp, switch_to="tag:ai-egress-jp2")
            bad = copy.deepcopy(READY_POLICY)
            bad["tagOwners"] = ["not", "a", "dict"]
            rc, _ = self.run_cplan(args, current=bad)
            self.assertEqual(rc, 1)
            self.assertTrue(list((Path(tmp) / "policy-plans").glob("failed.*")))

    def test_validate_api_failure_writes_failed_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.cplan_args(tmp, switch_to="tag:ai-egress-jp2")
            with mock.patch.object(
                tool, "tailscale_api",
                side_effect=[
                    (200, tool.dumps(READY_POLICY), "bearer", {"ETag": "e"}),
                    (400, "invalid", "bearer", {}),
                ],
            ):
                stdout = io.StringIO()
                with mock.patch.object(sys, "stdout", stdout):
                    rc = tool.connector_plan(args)
            self.assertEqual(rc, 1)
            self.assertIn("tailscale-api-validate", self.failed_finding_ids(self.only_failed(tmp)))

    def test_preview_failure_is_warn_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.cplan_args(tmp, switch_to="tag:ai-egress-jp2")
            with mock.patch.object(
                tool, "tailscale_api",
                side_effect=[
                    (200, tool.dumps(READY_POLICY), "bearer", {"ETag": "e"}),
                    (200, "ok", "bearer", {}),
                    (500, "no preview", "bearer", {}),
                ],
            ):
                stdout = io.StringIO()
                with mock.patch.object(sys, "stdout", stdout):
                    rc = tool.connector_plan(args)
            self.assertEqual(rc, 0)
            self.assertFalse((self.only_plan(tmp) / "api-preview.json").exists())

    # --- CONFIG-stage refusals ---
    def test_bad_target_tag_refuses_before_api_both_operations(self):
        for op in ({"switch_to": "tag:UPPER"}, {"declare": "tag:UPPER"}):
            with tempfile.TemporaryDirectory() as tmp:
                args = self.cplan_args(tmp, **op)
                with mock.patch.object(tool, "tailscale_api") as api:
                    stdout = io.StringIO()
                    with mock.patch.object(sys, "stdout", stdout):
                        rc = tool.connector_plan(args)
                self.assertEqual(rc, 1, op)
                api.assert_not_called()
                self.assertIn("connector-plan-target", self.failed_finding_ids(self.only_failed(tmp)), op)

    def test_cli_switch_and_declare_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit) as ctx:
            tool.main(["connector-plan", "--switch-to", "tag:a", "--declare", "tag:b"])
        self.assertEqual(ctx.exception.code, 2)
        with self.assertRaises(SystemExit) as ctx2:
            tool.main(["connector-plan"])
        self.assertEqual(ctx2.exception.code, 2)

    def test_bad_connector_name_on_switch_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.cplan_args(tmp, switch_to="tag:ai-egress-jp2", connector_name="bad name!")
            with mock.patch.object(tool, "tailscale_api") as api:
                stdout = io.StringIO()
                with mock.patch.object(sys, "stdout", stdout):
                    rc = tool.connector_plan(args)
            self.assertEqual(rc, 1)
            api.assert_not_called()
            self.assertIn("connector-name", self.failed_finding_ids(self.only_failed(tmp)))

    # --- declare ---
    def test_declare_happy_from_all_absent_parents(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.cplan_args(tmp, declare="tag:ai-egress-jp2")
            rc, _ = self.run_cplan(args, current={"nodeAttrs": []})
            self.assertEqual(rc, 0)
            merged = json.loads((self.only_plan(tmp) / "merged.json").read_text(encoding="utf-8"))
            # WHOLE-policy equality: any extra surface or key fails, not just the
            # three asserted ones.
            self.assertEqual(
                merged,
                {
                    "nodeAttrs": [],
                    "tagOwners": {"tag:ai-egress-jp2": ["autogroup:admin"]},
                    "autoApprovers": {
                        "routes": {"0.0.0.0/0": ["tag:ai-egress-jp2"], "::/0": ["tag:ai-egress-jp2"]}
                    },
                    "grants": [
                        {"src": ["autogroup:member"], "dst": ["tag:ai-egress-jp2"], "ip": ["tcp:53", "udp:53"]}
                    ],
                },
            )
            man = self.manifest_of(self.only_plan(tmp))
            self.assertEqual(man["operation"], "declare-pool")
            self.assertEqual(man["to"], "tag:ai-egress-jp2")
            self.assertNotIn("from", man)
            self.assertNotIn("connector_name", man)
            self.assertNotIn("domains_sha256", man)

    def test_declare_manifest_key_set_pin(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.cplan_args(tmp, declare="tag:ai-egress-jp2")
            self.run_cplan(args, current={"nodeAttrs": []})
            man = self.manifest_of(self.only_plan(tmp))
            expected = {
                "schema_version", "tool_version", "plan_id", "status", "created_at", "tailnet",
                "connector_tag", "current_sha256", "merged_sha256", "etag", "summary", "findings",
                "operation", "to",
            }
            self.assertEqual(set(man.keys()), expected)

    def test_declare_does_not_touch_connectors(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Declare a NOT-yet-declared pool (jp3) while a managed entry naming
            # jp exists; the connectors list must stay untouched.
            args = self.cplan_args(tmp, declare="tag:ai-egress-jp3")
            policy = copy.deepcopy(READY_POLICY)
            policy["nodeAttrs"][0]["app"][tool.APP_CONNECTORS_KEY][0]["connectors"] = ["tag:ai-egress-jp"]
            rc, _ = self.run_cplan(args, current=policy)
            self.assertEqual(rc, 0)
            merged = json.loads((self.only_plan(tmp) / "merged.json").read_text(encoding="utf-8"))
            self.assertEqual(
                merged["nodeAttrs"][0]["app"][tool.APP_CONNECTORS_KEY][0]["connectors"], ["tag:ai-egress-jp"]
            )

    def test_declare_partial_tagowners_present_only_routes_and_grant_added(self):
        policy = {"tagOwners": {"tag:ai-egress-jp2": ["autogroup:admin"]}}
        with tempfile.TemporaryDirectory() as tmp:
            args = self.cplan_args(tmp, declare="tag:ai-egress-jp2")
            rc, _ = self.run_cplan(args, current=policy)
            self.assertEqual(rc, 0)
            merged = json.loads((self.only_plan(tmp) / "merged.json").read_text(encoding="utf-8"))
            self.assertEqual(
                merged,
                {
                    "tagOwners": {"tag:ai-egress-jp2": ["autogroup:admin"]},
                    "autoApprovers": {
                        "routes": {"0.0.0.0/0": ["tag:ai-egress-jp2"], "::/0": ["tag:ai-egress-jp2"]}
                    },
                    "grants": [
                        {"src": ["autogroup:member"], "dst": ["tag:ai-egress-jp2"], "ip": ["tcp:53", "udp:53"]}
                    ],
                },
            )

    def test_declare_route_preservation_unions_not_replaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.cplan_args(tmp, declare="tag:ai-egress-jp2")
            policy = {"autoApprovers": {"routes": {"0.0.0.0/0": ["tag:unrelated"]}}}
            rc, _ = self.run_cplan(args, current=policy)
            self.assertEqual(rc, 0)
            merged = json.loads((self.only_plan(tmp) / "merged.json").read_text(encoding="utf-8"))
            self.assertEqual(merged["autoApprovers"]["routes"]["0.0.0.0/0"], ["tag:unrelated", "tag:ai-egress-jp2"])

    def test_declare_already_ready_refuses(self):
        # Build a policy already carrying all three surfaces.
        ready = {
            "tagOwners": {"tag:ai-egress-jp2": ["autogroup:admin"]},
            "autoApprovers": {"routes": {"0.0.0.0/0": ["tag:ai-egress-jp2"], "::/0": ["tag:ai-egress-jp2"]}},
            "grants": [{"src": ["autogroup:member"], "dst": ["tag:ai-egress-jp2"], "ip": ["tcp:53", "udp:53"]}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            args = self.cplan_args(tmp, declare="tag:ai-egress-jp2")
            rc, _ = self.run_cplan(args, current=ready)
            self.assertEqual(rc, 1)
            self.assertIn("declare-already-ready", self.failed_finding_ids(self.only_failed(tmp)))

    def test_declare_proceeds_when_tagowners_nonempty_but_missing_tag_owner(self):
        # Switch-readiness would pass, but declare must still add our owner.
        policy = {
            "tagOwners": {"tag:ai-egress-jp2": ["autogroup:other"]},
            "autoApprovers": {"routes": {"0.0.0.0/0": ["tag:ai-egress-jp2"], "::/0": ["tag:ai-egress-jp2"]}},
            "grants": [{"src": ["autogroup:member"], "dst": ["tag:ai-egress-jp2"], "ip": ["tcp:53", "udp:53"]}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            args = self.cplan_args(tmp, declare="tag:ai-egress-jp2")
            rc, _ = self.run_cplan(args, current=policy)
            self.assertEqual(rc, 0)
            merged = json.loads((self.only_plan(tmp) / "merged.json").read_text(encoding="utf-8"))
            self.assertEqual(merged["tagOwners"]["tag:ai-egress-jp2"], ["autogroup:other", "autogroup:admin"])

    def test_declare_proceeds_when_rich_dns_grant_satisfies_containment_but_canonical_grant_key_absent(self):
        policy = {
            "tagOwners": {"tag:ai-egress-jp2": ["autogroup:admin"]},
            "autoApprovers": {"routes": {"0.0.0.0/0": ["tag:ai-egress-jp2"], "::/0": ["tag:ai-egress-jp2"]}},
            # Containment-satisfying grant (extra ip) but not the canonical grant_key.
            "grants": [{"src": ["autogroup:member"], "dst": ["tag:ai-egress-jp2"], "ip": ["tcp:53", "udp:53", "tcp:443"]}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            args = self.cplan_args(tmp, declare="tag:ai-egress-jp2")
            rc, _ = self.run_cplan(args, current=policy)
            self.assertEqual(rc, 0)
            merged = json.loads((self.only_plan(tmp) / "merged.json").read_text(encoding="utf-8"))
            self.assertEqual(len(merged["grants"]), 2)  # canonical grant appended

    def test_declare_tripwire_extra_change_refuses(self):
        original = tool.mutate_declare

        def leaky(policy, tag, tag_owner, member_src):
            m = original(policy, tag, tag_owner, member_src)
            m["extraKey"] = "leak"
            return m

        with tempfile.TemporaryDirectory() as tmp:
            args = self.cplan_args(tmp, declare="tag:ai-egress-jp2")
            with mock.patch.object(tool, "mutate_declare", leaky):
                rc, _ = self.run_cplan(args, current={"nodeAttrs": []})
            self.assertEqual(rc, 1)
            self.assertIn("connector-only-diff", self.failed_finding_ids(self.only_failed(tmp)))

    def test_declare_with_expected_from_refuses_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.cplan_args(tmp, declare="tag:ai-egress-jp2", expected_from='["x"]')
            with mock.patch.object(tool, "tailscale_api") as api:
                stdout = io.StringIO()
                with mock.patch.object(sys, "stdout", stdout):
                    rc = tool.connector_plan(args)
            self.assertEqual(rc, 1)
            api.assert_not_called()
            self.assertIn("expected-from-misuse", self.failed_finding_ids(self.only_failed(tmp)))

    def test_declare_ignores_malformed_connector_name(self):
        # A garbage ambient connector-name must NOT block a declaration, and must
        # not be recorded in the declare manifest.
        with tempfile.TemporaryDirectory() as tmp:
            args = self.cplan_args(tmp, declare="tag:ai-egress-jp2", connector_name="bad name!")
            rc, _ = self.run_cplan(args, current={"nodeAttrs": []})
            self.assertEqual(rc, 0)
            self.assertNotIn("connector_name", self.manifest_of(self.only_plan(tmp)))


class ConnectorPlanListAndCompatTests(unittest.TestCase):
    def _write_manifest(self, plans_dir, plan_id, manifest):
        plan_dir = plans_dir / f"plan.{plan_id}"
        plan_dir.mkdir(parents=True)
        (plan_dir / "manifest.json").write_text(tool.dumps(manifest), encoding="utf-8")
        return plan_dir

    def _base(self, plan_id, **extra):
        man = {
            "schema_version": 1,
            "tool_version": tool.__version__,
            "plan_id": plan_id,
            "status": "valid",
            "created_at": "2026-09-02T00:00:00Z",
            "connector_tag": "tag:ai-egress-jp2",
        }
        man.update(extra)
        return man

    def test_list_plans_legacy_row_byte_identical(self):
        # A record WITHOUT operation must render exactly as before (no annotation).
        with tempfile.TemporaryDirectory() as tmp:
            plans_dir = Path(tmp)
            self._write_manifest(
                plans_dir, "20260527T143022Z-a1b2c3d4",
                self._base("20260527T143022Z-a1b2c3d4", connector_name="AI-Egress-JP"),
            )
            plan_path = plans_dir / "plan.20260527T143022Z-a1b2c3d4"
            args = argparse.Namespace(plans_dir=str(plans_dir), json=False)
            out = io.StringIO()
            with mock.patch.object(sys, "stdout", out):
                tool.list_policy_plans(args)
            body = out.getvalue().splitlines()[1]
            # BYTE golden for the whole legacy row: any annotation, spacing, or
            # column change fails.
            self.assertEqual(
                body,
                f"{'valid':<10} {'20260527T143022Z-a1b2c3d4':<26} {'2026-09-02T00:00:00Z':<22} "
                f"{'AI-Egress-JP':<20} {plan_path}",
            )
            # And the legacy JSON record carries no operation keys at all.
            args_json = argparse.Namespace(plans_dir=str(plans_dir), json=True)
            outj = io.StringIO()
            with mock.patch.object(sys, "stdout", outj):
                tool.list_policy_plans(args_json)
            rec = json.loads(outj.getvalue())["plans"][0]
            self.assertEqual(
                set(rec.keys()),
                {"status", "plan_id", "created_at", "connector_name", "connector_tag", "path"},
            )

    def test_list_plans_switch_annotation_text_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            plans_dir = Path(tmp)
            self._write_manifest(
                plans_dir, "20260527T143022Z-a1b2c3d4",
                self._base(
                    "20260527T143022Z-a1b2c3d4",
                    connector_name="AI-Egress-JP",
                    operation="switch-connectors",
                    **{"from": ["tag:ai-egress-jp"]},
                    to="tag:ai-egress-jp2",
                ),
            )
            args = argparse.Namespace(plans_dir=str(plans_dir), json=False)
            out = io.StringIO()
            with mock.patch.object(sys, "stdout", out):
                tool.list_policy_plans(args)
            self.assertIn('[switch-connectors: ["tag:ai-egress-jp"] -> tag:ai-egress-jp2]', out.getvalue())
            args_json = argparse.Namespace(plans_dir=str(plans_dir), json=True)
            outj = io.StringIO()
            with mock.patch.object(sys, "stdout", outj):
                tool.list_policy_plans(args_json)
            rec = json.loads(outj.getvalue())["plans"][0]
            self.assertEqual(rec["operation"], "switch-connectors")
            self.assertEqual(rec["from"], ["tag:ai-egress-jp"])
            self.assertEqual(rec["to"], "tag:ai-egress-jp2")

    def test_list_plans_declare_connector_column_falls_back_to_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            plans_dir = Path(tmp)
            self._write_manifest(
                plans_dir, "20260527T143022Z-a1b2c3d4",
                self._base("20260527T143022Z-a1b2c3d4", operation="declare-pool", to="tag:ai-egress-jp2"),
            )
            plan_path = plans_dir / "plan.20260527T143022Z-a1b2c3d4"
            args = argparse.Namespace(plans_dir=str(plans_dir), json=False)
            out = io.StringIO()
            with mock.patch.object(sys, "stdout", out):
                tool.list_policy_plans(args)
            body = out.getvalue().splitlines()[1]
            # Full-row golden: the CONNECTOR column itself carries the tag (not
            # merely the annotation), and the annotation renders after PATH.
            self.assertEqual(
                body,
                f"{'valid':<10} {'20260527T143022Z-a1b2c3d4':<26} {'2026-09-02T00:00:00Z':<22} "
                f"{'tag:ai-egress-jp2':<20} {plan_path} [declare-pool: tag:ai-egress-jp2]",
            )

    def test_list_plans_declare_record_has_no_from_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            plans_dir = Path(tmp)
            self._write_manifest(
                plans_dir, "20260527T143022Z-a1b2c3d4",
                self._base("20260527T143022Z-a1b2c3d4", operation="declare-pool", to="tag:ai-egress-jp2"),
            )
            args = argparse.Namespace(plans_dir=str(plans_dir), json=True)
            out = io.StringIO()
            with mock.patch.object(sys, "stdout", out):
                tool.list_policy_plans(args)
            rec = json.loads(out.getvalue())["plans"][0]
            self.assertNotIn("from", rec)

    def test_list_plans_switch_absent_connectors_from_is_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            plans_dir = Path(tmp)
            self._write_manifest(
                plans_dir, "20260527T143022Z-a1b2c3d4",
                self._base(
                    "20260527T143022Z-a1b2c3d4",
                    operation="switch-connectors", **{"from": None}, to="tag:ai-egress-jp2",
                ),
            )
            args = argparse.Namespace(plans_dir=str(plans_dir), json=True)
            out = io.StringIO()
            with mock.patch.object(sys, "stdout", out):
                tool.list_policy_plans(args)
            rec = json.loads(out.getvalue())["plans"][0]
            self.assertIn("from", rec)
            self.assertIsNone(rec["from"])

    def test_list_plans_defensive_recognized_operation_missing_companion_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            plans_dir = Path(tmp)
            self._write_manifest(
                plans_dir, "20260527T143022Z-a1b2c3d4",
                self._base("20260527T143022Z-a1b2c3d4", operation="switch-connectors"),  # no from/to
            )
            args = argparse.Namespace(plans_dir=str(plans_dir), json=False)
            out = io.StringIO()
            with mock.patch.object(sys, "stdout", out):
                rc = tool.list_policy_plans(args)
            self.assertEqual(rc, 0)  # did not crash
            self.assertIn("switch-connectors", out.getvalue())

    def test_list_plans_defensive_unknown_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            plans_dir = Path(tmp)
            self._write_manifest(
                plans_dir, "20260527T143022Z-a1b2c3d4",
                self._base("20260527T143022Z-a1b2c3d4", operation="teleport-pool", to="tag:x"),
            )
            args = argparse.Namespace(plans_dir=str(plans_dir), json=False)
            out = io.StringIO()
            with mock.patch.object(sys, "stdout", out):
                rc = tool.list_policy_plans(args)
            self.assertEqual(rc, 0)
            self.assertIn("[teleport-pool]", out.getvalue())

    def test_apply_plan_accepts_connector_plan_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = ConnectorPlanTests().cplan_args(tmp, switch_to="tag:ai-egress-jp2")
            ConnectorPlanTests().run_cplan(args, current=READY_POLICY)
            plan_dir = list((Path(tmp) / "policy-plans").glob("plan.*"))[0]
            apply_args = argparse.Namespace(
                plan_dir=str(plan_dir), tailnet=None, api_key="tskey-api-test",
                oauth_client_id=None, oauth_client_secret=None, oauth_scopes=None,
                prompt_token=False, yes=True,
            )
            with mock.patch.dict(os.environ, {"POLICY_RISK_ACK": "1"}):
                with mock.patch.object(
                    tool, "tailscale_api",
                    side_effect=[(200, "ok", "bearer", {}), (200, "{}", "bearer", {})],
                ):
                    rc = tool.apply_policy_plan(apply_args)
            self.assertEqual(rc, 0)


MERGE_GOLDEN = (
    '{\n  "grants": [\n    {\n      "src": [\n        "autogroup:member"\n      ],\n'
    '      "dst": [\n        "autogroup:internet"\n      ],\n      "ip": [\n        "*"\n      ]\n    },\n'
    '    {\n      "src": [\n        "autogroup:member"\n      ],\n      "dst": [\n'
    '        "tag:ai-egress-jp"\n      ],\n      "ip": [\n        "tcp:53",\n        "udp:53"\n      ]\n    }\n'
    '  ],\n  "tagOwners": {\n    "tag:ai-egress-jp": [\n      "autogroup:admin"\n    ]\n  },\n'
    '  "autoApprovers": {\n    "routes": {\n      "0.0.0.0/0": [\n        "tag:ai-egress-jp"\n      ],\n'
    '      "::/0": [\n        "tag:ai-egress-jp"\n      ]\n    }\n  },\n  "nodeAttrs": [\n    {\n'
    '      "target": [\n        "*"\n      ],\n      "app": {\n        "tailscale.com/app-connectors": [\n'
    '          {\n            "name": "AI-Egress-JP",\n            "connectors": [\n'
    '              "tag:ai-egress-jp"\n            ],\n            "domains": [\n'
    '              "chatgpt.com"\n            ]\n          }\n        ]\n      }\n    }\n  ]\n}\n'
)


class ConnectorStateTests(unittest.TestCase):
    """Direct tests for the read-only connector-state subcommand."""

    def cs_args(self, *, tags=None, connector_name="AI-Egress-JP"):
        return argparse.Namespace(
            connector_name=connector_name,
            tag=tags,
            member_src="autogroup:member",
            tailnet="-",
            api_key="tskey-api-test",
            oauth_client_id=None,
            oauth_client_secret=None,
            oauth_scopes=None,
            prompt_token=False,
        )

    def run_cs(self, args, *, policy, headers):
        with mock.patch.object(
            tool, "tailscale_api",
            side_effect=[(200, tool.dumps(policy), "bearer", headers)],
        ):
            out = io.StringIO()
            with mock.patch.object(sys, "stdout", out):
                rc = tool.connector_state(args)
        return rc, json.loads(out.getvalue())

    def test_connector_state_renders_facts_and_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                rc, doc = self.run_cs(
                    self.cs_args(tags=["tag:ai-egress-jp2", "tag:nope"]),
                    policy=READY_POLICY, headers={"ETag": "e9"},
                )
            finally:
                os.chdir(cwd)
            self.assertEqual(rc, 0)
            self.assertEqual(doc["etag"], "e9")
            self.assertEqual(doc["entry_count"], 1)
            self.assertEqual(doc["connectors"], ["tag:ai-egress-jp"])
            self.assertTrue(doc["readiness"]["tag:ai-egress-jp2"]["ready"])
            self.assertFalse(doc["readiness"]["tag:nope"]["ready"])
            self.assertIsNotNone(doc["readiness"]["tag:nope"]["error"])
            # Read-only by construction: nothing written anywhere.
            self.assertEqual(os.listdir(tmp), [])

    def test_connector_state_missing_etag_is_null_not_error(self):
        rc, doc = self.run_cs(self.cs_args(tags=None), policy=READY_POLICY, headers={})
        self.assertEqual(rc, 0)
        self.assertIsNone(doc["etag"])
        self.assertEqual(doc["readiness"], {})

    def test_connector_state_entry_count_zero_and_many_are_facts(self):
        none_policy = {"nodeAttrs": []}
        rc, doc = self.run_cs(self.cs_args(tags=None), policy=none_policy, headers={"ETag": "e"})
        self.assertEqual(rc, 0)
        self.assertEqual(doc["entry_count"], 0)
        self.assertIsNone(doc["connectors"])
        many = copy.deepcopy(READY_POLICY)
        many["nodeAttrs"].append(
            {"target": ["*"], "app": {tool.APP_CONNECTORS_KEY: [{"name": "AI-Egress-JP", "connectors": ["tag:x"]}]}}
        )
        rc2, doc2 = self.run_cs(self.cs_args(tags=None), policy=many, headers={"ETag": "e"})
        self.assertEqual(rc2, 0)
        self.assertEqual(doc2["entry_count"], 2)
        self.assertIsNone(doc2["connectors"])  # not unique -> no value claimed

    def test_connector_state_absent_connectors_key_is_null(self):
        policy = copy.deepcopy(READY_POLICY)
        del policy["nodeAttrs"][0]["app"][tool.APP_CONNECTORS_KEY][0]["connectors"]
        rc, doc = self.run_cs(self.cs_args(tags=None), policy=policy, headers={"ETag": "e"})
        self.assertEqual(rc, 0)
        self.assertEqual(doc["entry_count"], 1)
        self.assertIsNone(doc["connectors"])

    def test_connector_state_shape_invalid_policy_raises(self):
        bad = {"tagOwners": ["not", "a", "dict"]}
        with mock.patch.object(
            tool, "tailscale_api",
            side_effect=[(200, tool.dumps(bad), "bearer", {"ETag": "e"})],
        ):
            with self.assertRaises(tool.PolicyError):
                tool.connector_state(self.cs_args(tags=None))

    def test_connector_state_fetch_failure_raises(self):
        with mock.patch.object(
            tool, "tailscale_api",
            side_effect=[(500, "boom", "bearer", {})],
        ):
            with self.assertRaises(tool.PolicyError):
                tool.connector_state(self.cs_args(tags=None))


class ConnectorPlanStabilityTests(unittest.TestCase):
    def test_merge_policy_golden_byte_identical(self):
        merged = tool.merge_policy(
            {"grants": []},
            connector_name="AI-Egress-JP",
            connector_tag="tag:ai-egress-jp",
            domains=["chatgpt.com"],
            tag_owner="autogroup:admin",
            member_src="autogroup:member",
        )
        # BYTE-identical serialized output: the ordinary additive merge is
        # untouched by the connector-plan work.
        self.assertEqual(tool.dumps(merged), MERGE_GOLDEN)

    def test_ordinary_merge_reunions_old_pool_after_switch(self):
        # The documented post-switch merge hazard (Model B warning 3): an
        # ordinary full-merge whose input still names the old tag re-unions it
        # into connectors (dual-active). This pins the hazard the operator docs
        # warn about as a real, tested property.
        policy = {
            "nodeAttrs": [
                {
                    "target": ["*"],
                    "app": {
                        tool.APP_CONNECTORS_KEY: [
                            {"name": "AI-Egress-JP", "connectors": ["tag:ai-egress-jp2"], "domains": []}
                        ]
                    },
                }
            ]
        }
        merged = tool.merge_policy(
            copy.deepcopy(policy),
            connector_name="AI-Egress-JP",
            connector_tag="tag:ai-egress-jp",
            domains=[],
            tag_owner="autogroup:admin",
            member_src="autogroup:member",
        )
        self.assertEqual(
            merged["nodeAttrs"][0]["app"][tool.APP_CONNECTORS_KEY][0]["connectors"],
            ["tag:ai-egress-jp2", "tag:ai-egress-jp"],
        )

    def test_connector_plan_never_calls_merge_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            for op in ({"switch_to": "tag:ai-egress-jp2"}, {"declare": "tag:ai-egress-jp2"}):
                args = ConnectorPlanTests().cplan_args(tmp, **op)
                current = READY_POLICY if "switch_to" in op else {"nodeAttrs": []}
                with mock.patch.object(tool, "merge_policy", side_effect=AssertionError("merge_policy called")):
                    ConnectorPlanTests().run_cplan(args, current=current)

    def test_handle_merge_does_not_call_connector_plan_helpers(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "policy.hujson"
            src.write_text("{}\n", encoding="utf-8")
            domains = Path(tmp) / "domains.txt"
            domains.write_text("chatgpt.com\n", encoding="utf-8")
            args = argparse.Namespace(
                input=str(src), output=None, diff=False, report=None,
                domains_file=str(domains), connector_name="AI-Egress-JP",
                connector_tag="tag:ai-egress-jp", tag_owner="autogroup:admin",
                member_src="autogroup:member", allow_broad_wildcard=False,
            )
            with mock.patch.object(tool, "mutate_switch", side_effect=AssertionError("mutate_switch called")):
                with mock.patch.object(tool, "mutate_declare", side_effect=AssertionError("mutate_declare called")):
                    out = io.StringIO()
                    with mock.patch.object(sys, "stdout", out):
                        rc = tool.handle_merge(args)
            self.assertEqual(rc, 0)

    def test_restore_plan_reads_connector_plan_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = ConnectorPlanTests().cplan_args(tmp, switch_to="tag:ai-egress-jp2")
            ConnectorPlanTests().run_cplan(args, current=READY_POLICY)
            plan_dir = list((Path(tmp) / "policy-plans").glob("plan.*"))[0]
            # Mark applied so restore-plan accepts it, as the real flow would.
            manifest = json.loads((plan_dir / "manifest.json").read_text(encoding="utf-8"))
            tool.update_manifest_status(plan_dir, manifest, "applied", "applied_at")
            restore_args = argparse.Namespace(
                plan_dir=str(plan_dir), tailnet=None, api_key="tskey-api-test",
                oauth_client_id=None, oauth_client_secret=None, oauth_scopes=None,
                prompt_token=False, yes=True,
            )
            with mock.patch.dict(os.environ, {"POLICY_RISK_ACK": "1"}):
                with mock.patch.object(
                    tool, "tailscale_api",
                    side_effect=[
                        (200, "ok", "bearer", {}),
                        (200, "{}", "bearer", {"ETag": "fresh"}),
                        (200, "{}", "bearer", {}),
                    ],
                ):
                    rc = tool.restore_policy_plan(restore_args)
            self.assertEqual(rc, 0)

    def test_switch_guard_is_type_strict(self):
        # A mutation that flips an unrelated JSON 1 to true must be refused:
        # plain == treats them as equal, the type-strict guard must not.
        policy = copy.deepcopy(READY_POLICY)
        policy["randomSetting"] = 1
        original = tool.mutate_switch

        def flipper(pol, coords, target):
            m = original(pol, coords, target)
            m["randomSetting"] = True
            return m

        with tempfile.TemporaryDirectory() as tmp:
            args = ConnectorPlanTests().cplan_args(tmp, switch_to="tag:ai-egress-jp2")
            with mock.patch.object(tool, "mutate_switch", flipper):
                rc, _ = ConnectorPlanTests().run_cplan(args, current=policy)
            self.assertEqual(rc, 1)
            failed = list((Path(tmp) / "policy-plans").glob("failed.*"))
            self.assertEqual(len(failed), 1)
            report = json.loads((failed[0] / "report.invalid.json").read_text(encoding="utf-8"))
            self.assertIn("connector-only-diff", [f["id"] for f in report["findings"] if f["status"] == "fail"])

    def test_declare_guard_is_type_strict(self):
        policy = {"nodeAttrs": [], "randomSetting": 1}
        original = tool.mutate_declare

        def flipper(pol, tag, owner, msrc):
            m = original(pol, tag, owner, msrc)
            m["randomSetting"] = True
            return m

        with tempfile.TemporaryDirectory() as tmp:
            args = ConnectorPlanTests().cplan_args(tmp, declare="tag:ai-egress-jp2")
            with mock.patch.object(tool, "mutate_declare", flipper):
                rc, _ = ConnectorPlanTests().run_cplan(args, current=policy)
            self.assertEqual(rc, 1)
            failed = list((Path(tmp) / "policy-plans").glob("failed.*"))
            self.assertEqual(len(failed), 1)
            report = json.loads((failed[0] / "report.invalid.json").read_text(encoding="utf-8"))
            self.assertIn("connector-only-diff", [f["id"] for f in report["findings"] if f["status"] == "fail"])

    def test_create_policy_plan_does_not_call_connector_plan_helpers(self):
        with tempfile.TemporaryDirectory() as tmp:
            domains = Path(tmp) / "domains.txt"
            domains.write_text("chatgpt.com\n", encoding="utf-8")
            args = argparse.Namespace(
                domains_file=str(domains), connector_name="AI-Egress-JP", connector_tag="tag:ai-egress-jp",
                tag_owner="autogroup:admin", member_src="autogroup:member", allow_broad_wildcard=False,
                tailnet="-", api_key="tskey-api-test", oauth_client_id=None, oauth_client_secret=None,
                oauth_scopes=None, prompt_token=False, plans_dir=str(Path(tmp) / "policy-plans"),
            )
            with mock.patch.object(tool, "mutate_switch", side_effect=AssertionError("mutate_switch called")):
                with mock.patch.object(tool, "mutate_declare", side_effect=AssertionError("mutate_declare called")):
                    with mock.patch.object(
                        tool, "tailscale_api",
                        side_effect=[
                            (200, "{}\n", "bearer", {"ETag": "e"}),
                            (200, "ok", "bearer", {}),
                            (200, "{}\n", "bearer", {}),
                        ],
                    ):
                        out = io.StringIO()
                        with mock.patch.object(sys, "stdout", out):
                            rc = tool.create_policy_plan(args)
            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
