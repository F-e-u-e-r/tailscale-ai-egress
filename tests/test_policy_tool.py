import argparse
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

    def test_apply_dry_run_fetches_validates_and_reports_without_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            domains = Path(tmp) / "domains.txt"
            domains.write_text("chatgpt.com\n", encoding="utf-8")
            args = argparse.Namespace(
                domains_file=str(domains),
                connector_name="AI-Egress-JP",
                connector_tag="tag:ai-egress-jp",
                tag_owner="autogroup:admin",
                member_src="autogroup:member",
                allow_broad_wildcard=False,
                report="json",
                tailnet="-",
                api_key="tskey-api-test",
                oauth_client_id=None,
                oauth_client_secret=None,
                oauth_scopes=None,
                prompt_token=False,
                backup_dir=tmp,
                output=None,
                dry_run=True,
                diff=False,
            )
            with mock.patch.object(
                tool,
                "tailscale_api",
                side_effect=[
                    (200, "{}\n", "bearer", {"ETag": "abc"}),
                    (200, "ok", "bearer", {}),
                ],
            ) as api:
                stdout = io.StringIO()
                stderr = io.StringIO()
                with mock.patch.object(sys, "stdout", stdout):
                    with mock.patch.object(sys, "stderr", stderr):
                        rc = tool.apply_policy(args)

        self.assertEqual(rc, 0)
        self.assertEqual(api.call_count, 2)
        self.assertIn('"tagOwners"', stdout.getvalue())
        report = json.loads(stderr.getvalue())
        self.assertTrue(any(item["id"] == "dry-run" for item in report["findings"]))

    def test_legacy_apply_uses_mixed_case_etag_for_if_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            domains = Path(tmp) / "domains.txt"
            domains.write_text("chatgpt.com\n", encoding="utf-8")
            args = argparse.Namespace(
                domains_file=str(domains),
                connector_name="AI-Egress-JP",
                connector_tag="tag:ai-egress-jp",
                tag_owner="autogroup:admin",
                member_src="autogroup:member",
                allow_broad_wildcard=False,
                report=None,
                tailnet="-",
                api_key="tskey-api-test",
                oauth_client_id=None,
                oauth_client_secret=None,
                oauth_scopes=None,
                prompt_token=False,
                backup_dir=tmp,
                output=None,
                dry_run=False,
                diff=False,
            )
            with mock.patch.object(
                tool,
                "tailscale_api",
                side_effect=[
                    (200, "{}\n", "bearer", {"Etag": "legacy-etag"}),
                    (200, "ok", "bearer", {}),
                    (200, "applied", "bearer", {}),
                ],
            ) as api:
                rc = tool.apply_policy(args)

        self.assertEqual(rc, 0)
        self.assertEqual(api.call_args_list[2].kwargs["extra_headers"], {"If-Match": "legacy-etag"})

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


if __name__ == "__main__":
    unittest.main()
