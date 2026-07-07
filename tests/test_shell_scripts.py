import os
import hashlib
import json
import select
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path

try:
    import pty
except ImportError:  # pragma: no cover - non-Unix platforms
    pty = None


ROOT = Path(__file__).resolve().parents[1]


class ShellScriptTests(unittest.TestCase):
    def test_shell_syntax(self):
        for script in [
            "bootstrap.sh",
            "check-client-routes.sh",
            "diagnose.sh",
            "disable-exit-node.sh",
            "enable-exit-node.sh",
            "failover-exit-node.sh",
            "monitor-connectors.sh",
            "install.sh",
            "restore-connector.sh",
            "rollback.sh",
            "scripts/maintainer/apply-github-ruleset.sh",
            "scripts/validation-e2e.sh",
        ]:
            with self.subTest(script=script):
                subprocess.run(["bash", "-n", str(ROOT / script)], check=True)

    def test_bootstrap_common_domain_pack_overrides_env_and_policy_default_stays_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self._bootstrap_dry_run_env(tmp)
            env["GENERATED_DIR"] = tmp
            env["TAILSCALE_API_KEY"] = "tskey-api-test"
            env["AI_EGRESS_DOMAINS_FILE"] = str(Path(tmp) / "missing-domains.txt")
            result = subprocess.run(
                ["bash", str(ROOT / "bootstrap.sh"), "--dry-run", "--domain-pack", "common"],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

        combined = result.stdout + result.stderr
        self.assertIn("Domain pack: common", combined)
        self.assertIn("chatgpt.com", combined)
        self.assertNotIn("Advanced Mode will add a broad grant", combined)
        self.assertNotIn("Tailscale policy validation passed", combined)

    def test_install_wrapper_uses_local_bootstrap_when_repo_present(self):
        result = subprocess.run(
            ["bash", str(ROOT / "install.sh"), "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )

        self.assertIn("Usage: ./bootstrap.sh", result.stdout)

    def test_install_wrapper_downloads_and_verifies_release_asset(self):
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        asset_name = f"tailscale-ai-egress-{version}.tar.gz"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            release_root = tmp_path / "release-src" / f"tailscale-ai-egress-{version}"
            release_root.mkdir(parents=True)
            bootstrap = release_root / "bootstrap.sh"
            bootstrap.write_text("#!/usr/bin/env bash\nprintf 'fake bootstrap %s\\n' \"$*\"\n", encoding="utf-8")
            bootstrap.chmod(0o755)

            asset = tmp_path / asset_name
            subprocess.run(["tar", "-czf", str(asset), "-C", str(tmp_path / "release-src"), release_root.name], check=True)
            digest = hashlib.sha256(asset.read_bytes()).hexdigest()
            sums = tmp_path / "SHA256SUMS"
            sums.write_text(f"{digest}  {asset_name}\n", encoding="utf-8")

            fake_bin = tmp_path / "bin"
            fake_bin.mkdir()
            self._write_fake_command(
                fake_bin,
                "curl",
                f"""#!/bin/sh
if [ "$1" != "-fsSLo" ]; then
  echo "unexpected curl args: $*" >&2
  exit 2
fi
out="$2"
url="$3"
case "$url" in
  */SHA256SUMS) cp {sums} "$out" ;;
  */{asset_name}) cp {asset} "$out" ;;
  *) echo "unexpected URL: $url" >&2; exit 2 ;;
esac
""",
            )
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
            result = subprocess.run(
                ["bash", str(ROOT / "install.sh"), "--dry-run"],
                cwd=tmp,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

        self.assertIn(f"Downloading https://github.com/F-e-u-e-r/tailscale-ai-egress/releases/download/v{version}/{asset_name}", result.stdout)
        self.assertIn(f"{asset_name}: OK", result.stdout)
        self.assertIn("fake bootstrap --dry-run", result.stdout)

    def test_bootstrap_uses_ai_egress_domains_file_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            domains = Path(tmp) / "custom-domains.txt"
            domains.write_text("example.ai\n", encoding="utf-8")
            env = self._bootstrap_dry_run_env(tmp)
            env["GENERATED_DIR"] = tmp
            env["AI_EGRESS_DOMAINS_FILE"] = str(domains)
            result = subprocess.run(
                ["bash", str(ROOT / "bootstrap.sh"), "--dry-run"],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

        combined = result.stdout + result.stderr
        self.assertIn(f"Domain file from AI_EGRESS_DOMAINS_FILE: {domains}", combined)
        self.assertIn("example.ai", combined)

    def test_bootstrap_dry_run_uses_default_identity_without_region_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp) / "bin"
            fake_bin.mkdir()
            curl_log = Path(tmp) / "curl.log"
            self._write_fake_command(
                fake_bin,
                "curl",
                f"""#!/bin/sh
echo "$@" >> {curl_log}
exit 0
""",
            )
            self._write_fake_command(
                fake_bin,
                "tailscale",
                """#!/bin/sh
if [ "$1" = "status" ] && [ "$2" = "--self" ]; then
  exit 1
fi
if [ "$1" = "version" ]; then
  echo "1.80.0-test"
  exit 0
fi
exit 0
""",
            )
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
            env["GENERATED_DIR"] = tmp
            result = subprocess.run(
                ["bash", str(ROOT / "bootstrap.sh"), "--dry-run"],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

        combined = result.stdout + result.stderr
        self.assertIn("Connector name:     AI-Egress-JP", combined)
        self.assertIn("Connector tag:      tag:ai-egress-jp", combined)
        self.assertIn("Connector hostname: ai-egress-jp-01", combined)
        self.assertIn("tailscale up --hostname=ai-egress-jp-01", result.stdout)
        self.assertFalse(curl_log.exists())

    def test_bootstrap_identity_env_overrides_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self._bootstrap_dry_run_env(tmp)
            env["GENERATED_DIR"] = tmp
            env["REGION"] = "us"
            env["CONNECTOR_NAME"] = "AI-Egress-Custom"
            env["CONNECTOR_TAG"] = "tag:ai-egress-custom"
            env["CONNECTOR_HOSTNAME"] = "ai-egress-custom-host"
            result = subprocess.run(
                ["bash", str(ROOT / "bootstrap.sh"), "--dry-run"],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

        combined = result.stdout + result.stderr
        self.assertIn("Connector name:     AI-Egress-Custom", combined)
        self.assertIn("Connector tag:      tag:ai-egress-custom", combined)
        self.assertIn("Connector hostname: ai-egress-custom-host", combined)
        self.assertIn("--advertise-tags=tag:ai-egress-custom", result.stdout)

    def test_bootstrap_hostname_keyword_normalizes_and_reprompts(self):
        result = subprocess.run(
            [
                "bash",
                "-c",
                f"""
source "{ROOT / "bootstrap.sh"}"
REGION_LOWER=jp
DRY_RUN=0
is_interactive() {{ return 0; }}
responses="$(mktemp)"
printf 'x\\nAB!CDE\\n' > "$responses"
exec 3< "$responses"
read_line() {{
  local response=''
  IFS= read -r response <&3 || response=''
  printf '%s' "$response"
}}
resolve_hostname
exec 3<&-
printf '%s\\n' "$CONNECTOR_HOSTNAME"
printf '%s\\n' "$(normalize_hostname_keyword 'A B-c_1')"
""",
            ],
            text=True,
            capture_output=True,
            check=True,
        )

        self.assertEqual(result.stdout.splitlines(), ["ai-egress-jp-abcde", "abc1"])
        self.assertIn("Hostname keyword must normalize to 3-5", result.stderr)

    def test_bootstrap_region_detection_default_can_be_accepted(self):
        result = subprocess.run(
            [
                "bash",
                "-c",
                f"""
source "{ROOT / "bootstrap.sh"}"
DRY_RUN=0
is_interactive() {{ return 0; }}
detect_region() {{ printf 'us'; }}
read_line() {{ printf ''; }}
resolve_region
printf '%s\\n' "$REGION"
""",
            ],
            text=True,
            capture_output=True,
            check=True,
        )

        self.assertEqual(result.stdout.splitlines(), ["Detected region: us", "us"])

    def test_bootstrap_noninteractive_region_auto_detects_without_jp_fallback(self):
        result = subprocess.run(
            [
                "bash",
                "-c",
                f"""
source "{ROOT / "bootstrap.sh"}"
DRY_RUN=0
is_interactive() {{ return 1; }}
detect_region() {{ printf 'tw'; }}
resolve_region
printf '%s\\n' "$REGION"
""",
            ],
            text=True,
            capture_output=True,
            check=True,
        )

        self.assertEqual(result.stdout.splitlines(), ["tw"])

    def test_bootstrap_noninteractive_region_requires_region_when_detection_fails(self):
        result = subprocess.run(
            [
                "bash",
                "-c",
                f"""
source "{ROOT / "bootstrap.sh"}"
DRY_RUN=0
is_interactive() {{ return 1; }}
detect_region() {{ printf ''; }}
resolve_region
""",
            ],
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("REGION is required", result.stderr)

    def test_bootstrap_persists_connector_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    f"""
source "{ROOT / "bootstrap.sh"}"
GENERATED_DIR="{tmp}"
REGION_LOWER=sg
CONNECTOR_TAG=tag:ai-egress-sg
CONNECTOR_HOSTNAME=ai-egress-sg-main
persist_connector_identity
cat "{tmp}/connector-identity.env"
""",
                ],
                text=True,
                capture_output=True,
                check=True,
            )

        self.assertIn("REGION=sg", result.stdout)
        self.assertIn("CONNECTOR_TAG=tag:ai-egress-sg", result.stdout)
        self.assertIn("CONNECTOR_HOSTNAME=ai-egress-sg-main", result.stdout)

    def test_bootstrap_warns_when_ai_egress_domains_file_falls_back_to_common(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self._bootstrap_dry_run_env(tmp)
            env["GENERATED_DIR"] = tmp
            env["AI_EGRESS_DOMAINS_FILE"] = str(Path(tmp) / "missing-domains.txt")
            result = subprocess.run(
                ["bash", str(ROOT / "bootstrap.sh"), "--dry-run"],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

        combined = result.stdout + result.stderr
        self.assertIn("Could not validate domain file from AI_EGRESS_DOMAINS_FILE", combined)
        self.assertIn("falling back to the common domain pack", combined)
        self.assertIn("Domain pack: common", combined)
        self.assertIn("chatgpt.com", combined)

    def test_bootstrap_rejects_domain_pack_and_domains_file_together(self):
        result = subprocess.run(
            [
                "bash",
                str(ROOT / "bootstrap.sh"),
                "--dry-run",
                "--domain-pack",
                "common",
                "--domains-file",
                str(ROOT / "policy/default-ai-domains.json"),
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("mutually exclusive", result.stderr)

    def test_bootstrap_rejects_unknown_domain_pack(self):
        result = subprocess.run(
            ["bash", str(ROOT / "bootstrap.sh"), "--dry-run", "--domain-pack", "unknown"],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Available packs: common", result.stderr)

    def test_diagnose_rejects_unknown_domain_pack_without_running_checks(self):
        result = subprocess.run(
            ["bash", str(ROOT / "diagnose.sh"), "--domain-pack", "unknown", "--json"],
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("Available packs: common", result.stderr)
        self.assertNotIn("schema_version", result.stdout)

    def test_bootstrap_rejects_invalid_region_env(self):
        for region in ("a--b", "us_east"):
            with self.subTest(region=region):
                env = os.environ.copy()
                env["REGION"] = region
                result = subprocess.run(
                    ["bash", str(ROOT / "bootstrap.sh"), "--dry-run"],
                    env=env,
                    text=True,
                    capture_output=True,
                )

                self.assertEqual(result.returncode, 1)
                self.assertIn("REGION must contain only letters, numbers, and single hyphens", result.stderr)

    def test_rollback_list_uses_generated_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            generated = Path(tmp)
            first = generated / "tailnet-policy.backup.20260526T010000Z.hujson"
            second = generated / "tailnet-policy.backup.20260526T020000Z.hujson"
            first.write_text("{}", encoding="utf-8")
            second.write_text("{}", encoding="utf-8")

            env = os.environ.copy()
            env["GENERATED_DIR"] = str(generated)
            result = subprocess.run(
                ["bash", str(ROOT / "rollback.sh"), "--list"],
                check=True,
                env=env,
                text=True,
                capture_output=True,
            )

        self.assertIn("20260526T010000Z", result.stdout)
        self.assertIn("20260526T020000Z", result.stdout)
        self.assertIn(str(first), result.stdout)
        self.assertIn(str(second), result.stdout)

    def test_rollback_reports_explicit_missing_backup_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["GENERATED_DIR"] = tmp
            missing = str(Path(tmp) / "tailnet-policy.backup.missing.hujson")
            result = subprocess.run(
                ["bash", str(ROOT / "rollback.sh"), missing],
                env=env,
                text=True,
                capture_output=True,
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn(f"backup not found: {missing}", result.stderr)

    def _write_fake_command(self, directory, name, body):
        path = Path(directory) / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def _bootstrap_dry_run_env(self, tmp, *, self_status=None):
        fake_bin = Path(tmp) / "bin"
        fake_bin.mkdir(exist_ok=True)
        status_block = "exit 1"
        if self_status is not None:
            status_block = f"echo {self_status!r}; exit 0"
        self._write_fake_command(
            fake_bin,
            "tailscale",
            f"""#!/bin/sh
if [ "$1" = "status" ] && [ "$2" = "--self" ]; then
  {status_block}
fi
if [ "$1" = "version" ]; then
  echo "1.80.0-test"
  exit 0
fi
exit 0
""",
        )
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
        return env

    def test_bootstrap_fresh_dry_run_omits_tailscale_up_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp) / "bin"
            fake_bin.mkdir()
            log = Path(tmp) / "commands.log"
            self._write_fake_command(
                fake_bin,
                "tailscale",
                f"""#!/bin/sh
echo "$@" >> {log}
if [ "$1" = "status" ] && [ "$2" = "--self" ]; then
  exit 1
fi
if [ "$1" = "version" ]; then
  echo "1.80.0-test"
  exit 0
fi
exit 0
""",
            )
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
            env["GENERATED_DIR"] = tmp
            result = subprocess.run(
                ["bash", str(ROOT / "bootstrap.sh"), "--dry-run", "--domain-pack", "common"],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

        self.assertIn("tailscale up --hostname=ai-egress-jp-01", result.stdout)
        self.assertNotIn("tailscale up --reset", result.stdout)

    def test_bootstrap_noninteractive_configured_differently_requires_ack(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self._bootstrap_dry_run_env(tmp, self_status="100.64.0.2 other-host tag:other")
            env["GENERATED_DIR"] = tmp
            result = subprocess.run(
                ["bash", str(ROOT / "bootstrap.sh"), "--dry-run", "--domain-pack", "common"],
                env=env,
                text=True,
                capture_output=True,
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("configured differently", result.stderr)
        self.assertIn("BOOTSTRAP_RESET_ACK=1", result.stderr)

    def test_bootstrap_noninteractive_configured_differently_ack_uses_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self._bootstrap_dry_run_env(tmp, self_status="100.64.0.2 other-host tag:other")
            env["GENERATED_DIR"] = tmp
            env["BOOTSTRAP_RESET_ACK"] = "1"
            result = subprocess.run(
                ["bash", str(ROOT / "bootstrap.sh"), "--dry-run", "--domain-pack", "common"],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

        self.assertIn("tailscale up --reset --hostname=ai-egress-jp-01", result.stdout)

    def test_bootstrap_noninteractive_same_hostname_without_ack_does_not_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self._bootstrap_dry_run_env(tmp, self_status="100.64.0.2 ai-egress-jp-01 tag:ai-egress-jp")
            env["GENERATED_DIR"] = tmp
            result = subprocess.run(
                ["bash", str(ROOT / "bootstrap.sh"), "--dry-run", "--domain-pack", "common"],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

        self.assertNotIn("tailscale up --reset", result.stdout)
        self.assertNotIn("tailscale up --hostname", result.stdout)

    def test_bootstrap_noninteractive_same_hostname_ack_uses_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self._bootstrap_dry_run_env(tmp, self_status="100.64.0.2 ai-egress-jp-01 tag:ai-egress-jp")
            env["GENERATED_DIR"] = tmp
            env["BOOTSTRAP_RESET_ACK"] = "1"
            result = subprocess.run(
                ["bash", str(ROOT / "bootstrap.sh"), "--dry-run", "--domain-pack", "common"],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

        self.assertIn("tailscale up --reset --hostname=ai-egress-jp-01", result.stdout)

    def test_bootstrap_noninteractive_hostname_prefix_is_not_treated_as_same(self):
        # A different host whose name merely contains the configured hostname must
        # NOT be matched as "same host". Exact whitespace-field matching rejects
        # both a trailing-digit case (ai-egress-jp-011, which a bare substring
        # match would wrongly accept) and a hyphen-prefixed case
        # (foo-ai-egress-jp-01, which a `grep -qwF` word match would wrongly
        # accept). Both must be treated as configured differently.
        for other_host in ("ai-egress-jp-011", "foo-ai-egress-jp-01"):
            with self.subTest(other_host=other_host):
                with tempfile.TemporaryDirectory() as tmp:
                    env = self._bootstrap_dry_run_env(
                        tmp, self_status=f"100.64.0.2 {other_host} tag:ai-egress-jp"
                    )
                    env["GENERATED_DIR"] = tmp
                    result = subprocess.run(
                        ["bash", str(ROOT / "bootstrap.sh"), "--dry-run", "--domain-pack", "common"],
                        env=env,
                        text=True,
                        capture_output=True,
                    )

                self.assertEqual(result.returncode, 1)
                self.assertIn("configured differently", result.stderr)

    def test_dev_tty_prompt_helpers_print_prompt_before_read(self):
        # Regression guard for the /dev/tty fallback: it must print the prompt to
        # the tty explicitly, not rely on `read -p ... </dev/tty` whose prompt
        # (written to stderr) is swallowed when stdin is piped.
        for name in ("bootstrap.sh", "enable-exit-node.sh", "restore-connector.sh"):
            with self.subTest(script=name):
                text = (ROOT / name).read_text(encoding="utf-8")
                self.assertNotIn('read -r -p "$prompt" answer </dev/tty', text)
                self.assertNotIn('read -r -s -p "$prompt" answer </dev/tty', text)
                self.assertIn("printf '%s' \"$prompt\" >/dev/tty", text)

    @unittest.skipUnless(pty is not None, "pty is unavailable on this platform")
    def test_read_line_prompt_visible_when_stdin_piped_but_tty_available(self):
        # End-to-end: source the real bootstrap.sh under a pseudo-terminal, call
        # read_line with stdin redirected from /dev/null (not a tty) while a
        # controlling tty exists. The prompt must reach the tty and the typed
        # answer must be captured. The pre-fix code swallowed the prompt.
        script = (
            f'source "{ROOT / "bootstrap.sh"}"\n'
            'answer="$(read_line "PROMPT_MARKER> " </dev/null)"\n'
            'printf "CAPTURED[%s]\\n" "$answer" >/dev/tty\n'
        )
        pid, fd = pty.fork()
        if pid == 0:  # pragma: no cover - child process
            os.execvp("bash", ["bash", "-c", script])
            os._exit(127)

        output = b""
        try:
            os.write(fd, b"typed-answer\n")
            while True:
                try:
                    ready, _, _ = select.select([fd], [], [], 10)
                except OSError:
                    break
                if not ready:
                    break
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                output += data
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                os.waitpid(pid, 0)
            except OSError:
                pass
            os.close(fd)

        text = output.decode(errors="replace")
        self.assertIn("PROMPT_MARKER>", text)
        self.assertIn("CAPTURED[typed-answer]", text)

    def test_monitor_usage_marks_api_key_environment_only(self):
        result = subprocess.run(
            ["bash", str(ROOT / "monitor-connectors.sh"), "--help"],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("Environment only", result.stdout)
        self.assertIn("TAILSCALE_API_KEY", result.stdout)

    def test_install_local_checkout_announces_itself(self):
        # From a checkout, install.sh must announce that it runs the local
        # bootstrap.sh instead of doing it silently.
        result = subprocess.run(
            ["bash", str(ROOT / "install.sh"), "--help"],
            text=True,
            capture_output=True,
            cwd=ROOT,
        )
        self.assertIn("executing local checkout", result.stderr)

    def _helper_env(self, tmp, *, status_json=None, connector_set_supported=True, status_after_mutation=None):
        fake_bin = Path(tmp) / "bin"
        fake_bin.mkdir()
        log = Path(tmp) / "commands.log"
        if status_json is None:
            status_json = {
                "Self": {
                    "Online": True,
                    "Tags": ["tag:ai-egress-jp"],
                    "HostName": "ai-egress-jp-01",
                    "AllowedIPs": [],
                }
            }
        status_file = Path(tmp) / "status.json"
        status_file.write_text(json.dumps(status_json), encoding="utf-8")
        mutation_status_update = ""
        if status_after_mutation is not None:
            status_after_mutation_file = Path(tmp) / "status-after-mutation.json"
            status_after_mutation_file.write_text(status_after_mutation, encoding="utf-8")
            mutation_status_update = f"cp {status_after_mutation_file} {status_file}"
        unsupported_block = ""
        if not connector_set_supported:
            unsupported_block = """
if [ "$1" = "set" ] && [ "$2" = "--advertise-connector" ]; then
  echo "unknown flag: --advertise-connector" >&2
  exit 1
fi
"""

        self._write_fake_command(
            fake_bin,
            "tailscale",
            f"""#!/bin/sh
echo "$@" >> {log}
if [ "$1" = "status" ] && [ "$2" = "--json" ]; then
  cat {status_file}
  exit 0
fi
if [ "$1" = "status" ]; then
  echo "100.64.0.1 ai-egress-jp-01 tag:ai-egress-jp"
  exit 0
fi
if [ "$1" = "version" ]; then
  echo "1.80.0-test"
  exit 0
fi
{unsupported_block}
if [ "$1" = "set" ] || [ "$1" = "up" ]; then
  {mutation_status_update}
  exit 0
fi
exit 0
""",
        )
        self._write_fake_command(fake_bin, "uname", "#!/bin/sh\necho Linux\n")
        self._write_fake_command(
            fake_bin,
            "sysctl",
            f"""#!/bin/sh
echo "$@" >> {log}
if [ "$1" = "-n" ]; then
  case "$2" in
    net.ipv4.ip_forward|net.ipv6.conf.all.forwarding) echo 1 ;;
  esac
  exit 0
fi
if [ "$1" = "net.ipv6.conf.all.forwarding" ]; then
  echo "net.ipv6.conf.all.forwarding = 1"
  exit 0
fi
exit 0
""",
        )
        self._write_fake_command(fake_bin, "install", f"#!/bin/sh\necho \"$@\" >> {log}\nexit 0\n")

        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
        env["AI_EGRESS_USE_SUDO"] = "0"
        return env, log

    def test_enable_exit_node_dry_run_generates_forwarding_and_set_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, _log = self._helper_env(tmp)
            env["EXIT_NODE_ACK"] = "1"
            result = subprocess.run(
                ["bash", str(ROOT / "enable-exit-node.sh"), "--dry-run"],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

        self.assertIn("tailscale set --advertise-exit-node", result.stdout)
        self.assertIn("sysctl -w net.ipv4.ip_forward=1", result.stdout)

    def test_enable_exit_node_detects_dynamic_connector_tag_from_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, _log = self._helper_env(
                tmp,
                status_json={
                    "Self": {
                        "Online": True,
                        "Tags": ["tag:ai-egress-us"],
                        "HostName": "ai-egress-us-abc",
                        "AllowedIPs": [],
                    }
                },
            )
            env["EXIT_NODE_ACK"] = "1"
            result = subprocess.run(
                ["bash", str(ROOT / "enable-exit-node.sh"), "--dry-run"],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

        self.assertIn("tailscale set --advertise-exit-node", result.stdout)
        self.assertNotIn("tag:ai-egress-jp", result.stdout + result.stderr)

    def test_enable_exit_node_refuses_ambiguous_status_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log = self._helper_env(
                tmp,
                status_json={
                    "Self": {
                        "Online": True,
                        "Tags": ["tag:ai-egress-us", "tag:ai-egress-tw"],
                        "HostName": "ai-egress-us-abc",
                        "AllowedIPs": [],
                    }
                },
            )
            env["EXIT_NODE_ACK"] = "1"
            result = subprocess.run(
                ["bash", str(ROOT / "enable-exit-node.sh"), "--dry-run"],
                env=env,
                text=True,
                capture_output=True,
            )

            commands = log.read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 1)
        self.assertIn("multiple tag:ai-egress-* tags", result.stderr)
        self.assertNotIn("set --advertise-exit-node", commands)

    def test_enable_exit_node_refuses_stale_persisted_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            generated = Path(tmp) / "generated"
            generated.mkdir()
            (generated / "connector-identity.env").write_text(
                "REGION=tw\nCONNECTOR_TAG=tag:ai-egress-tw\nCONNECTOR_HOSTNAME=ai-egress-tw-main\n",
                encoding="utf-8",
            )
            env, log = self._helper_env(
                tmp,
                status_json={
                    "Self": {
                        "Online": True,
                        "Tags": ["tag:ai-egress-us"],
                        "HostName": "ai-egress-us-abc",
                        "AllowedIPs": [],
                    }
                },
            )
            env["GENERATED_DIR"] = str(generated)
            env["EXIT_NODE_ACK"] = "1"
            result = subprocess.run(
                ["bash", str(ROOT / "enable-exit-node.sh"), "--dry-run"],
                env=env,
                text=True,
                capture_output=True,
            )

            commands = log.read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 1)
        self.assertIn("conflicts with current Tailscale status tag", result.stderr)
        self.assertNotIn("set --advertise-exit-node", commands)

    def test_enable_exit_node_refuses_without_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, _log = self._helper_env(tmp)
            result = subprocess.run(
                ["bash", str(ROOT / "enable-exit-node.sh"), "--dry-run"],
                env=env,
                text=True,
                capture_output=True,
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("Cancelled; exit-node fallback was not enabled", result.stderr)

    def test_enable_exit_node_refuses_non_connector_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, _log = self._helper_env(tmp, status_json={"Self": {"Online": True, "Tags": []}})
            env["EXIT_NODE_ACK"] = "1"
            result = subprocess.run(
                ["bash", str(ROOT / "enable-exit-node.sh"), "--dry-run"],
                env=env,
                text=True,
                capture_output=True,
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("Could not identify a connector tag", result.stderr)
        self.assertNotIn("tag:ai-egress-jp", result.stderr)

    def test_enable_exit_node_degrades_gracefully_on_malformed_status_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log = self._helper_env(tmp)
            (Path(tmp) / "status.json").write_text("{not valid json", encoding="utf-8")
            env["CONNECTOR_TAG"] = "tag:ai-egress-jp"
            env["EXIT_NODE_ACK"] = "1"
            result = subprocess.run(
                ["bash", str(ROOT / "enable-exit-node.sh"), "--dry-run"],
                env=env,
                text=True,
                capture_output=True,
            )

            commands = log.read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 1)
        self.assertIn("Could not parse Tailscale status JSON", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("set --advertise-exit-node", commands)

    def test_enable_exit_node_refuses_when_tailscale_not_logged_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp) / "bin"
            fake_bin.mkdir()
            self._write_fake_command(fake_bin, "uname", "#!/bin/sh\necho Linux\n")
            self._write_fake_command(
                fake_bin,
                "tailscale",
                """#!/bin/sh
if [ "$1" = "status" ]; then exit 1; fi
exit 0
""",
            )
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
            env["EXIT_NODE_ACK"] = "1"
            result = subprocess.run(
                ["bash", str(ROOT / "enable-exit-node.sh"), "--dry-run"],
                env=env,
                text=True,
                capture_output=True,
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("Tailscale is not running or this host is not logged in", result.stderr)

    def test_enable_exit_node_post_verify_reports_advertised(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log = self._helper_env(
                tmp,
                status_json={
                    "Self": {
                        "Online": True,
                        "Tags": ["tag:ai-egress-jp"],
                        "AllowedIPs": ["0.0.0.0/0", "::/0"],
                    }
                },
            )
            env["EXIT_NODE_ACK"] = "1"
            result = subprocess.run(
                ["bash", str(ROOT / "enable-exit-node.sh")],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            commands = log.read_text(encoding="utf-8")

        self.assertIn("set --advertise-exit-node", commands)
        self.assertIn("Exit-node advertising is visible", result.stdout)

    def test_enable_exit_node_post_verify_warns_on_malformed_status_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log = self._helper_env(tmp, status_after_mutation="{not valid json")
            env["EXIT_NODE_ACK"] = "1"
            result = subprocess.run(
                ["bash", str(ROOT / "enable-exit-node.sh")],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            commands = log.read_text(encoding="utf-8")

        self.assertIn("set --advertise-exit-node", commands)
        self.assertIn("verification was skipped", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_disable_exit_node_warns_when_connector_tag_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log = self._helper_env(tmp, status_json={"Self": {"Online": True, "Tags": []}})
            result = subprocess.run(
                ["bash", str(ROOT / "disable-exit-node.sh")],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            commands = log.read_text(encoding="utf-8")

        self.assertIn("set --advertise-exit-node=false", commands)
        self.assertIn("connector tag verification was skipped", result.stderr)
        self.assertNotIn("tag:ai-egress-jp", result.stderr)

    def test_disable_exit_node_refuses_ambiguous_status_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log = self._helper_env(
                tmp,
                status_json={
                    "Self": {
                        "Online": True,
                        "Tags": ["tag:ai-egress-us", "tag:ai-egress-tw"],
                    }
                },
            )
            result = subprocess.run(
                ["bash", str(ROOT / "disable-exit-node.sh")],
                env=env,
                text=True,
                capture_output=True,
            )

            commands = log.read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 1)
        self.assertIn("multiple tag:ai-egress-* tags", result.stderr)
        self.assertNotIn("set --advertise-exit-node=false", commands)

    def test_disable_exit_node_refuses_malformed_status_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log = self._helper_env(tmp)
            (Path(tmp) / "status.json").write_text("{not valid json", encoding="utf-8")
            result = subprocess.run(
                ["bash", str(ROOT / "disable-exit-node.sh")],
                env=env,
                text=True,
                capture_output=True,
            )

            commands = log.read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 1)
        self.assertIn("Could not parse Tailscale status JSON", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("set --advertise-exit-node=false", commands)

    def test_disable_exit_node_post_verify_warns_on_malformed_status_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log = self._helper_env(tmp, status_after_mutation="{not valid json")
            result = subprocess.run(
                ["bash", str(ROOT / "disable-exit-node.sh")],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            commands = log.read_text(encoding="utf-8")

        self.assertIn("set --advertise-exit-node=false", commands)
        self.assertIn("verification was skipped", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_restore_connector_warns_when_set_connector_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log = self._helper_env(tmp, connector_set_supported=False)
            result = subprocess.run(
                ["bash", str(ROOT / "restore-connector.sh")],
                env=env,
                text=True,
                capture_output=True,
            )

            commands = log.read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 1)
        self.assertIn("set --advertise-exit-node=false", commands)
        self.assertIn("does not support 'tailscale set --advertise-connector'", result.stderr)
        self.assertIn("--force-reset", result.stderr)

    def test_restore_connector_default_does_not_block_on_stale_identity_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            generated = Path(tmp) / "generated"
            generated.mkdir()
            (generated / "connector-identity.env").write_text(
                "REGION=tw\nCONNECTOR_TAG=tag:ai-egress-tw\nCONNECTOR_HOSTNAME=ai-egress-tw-main\n",
                encoding="utf-8",
            )
            env, log = self._helper_env(
                tmp,
                status_json={"Self": {"Online": True, "Tags": [], "HostName": "replacement-host"}},
            )
            env["GENERATED_DIR"] = str(generated)
            result = subprocess.run(
                ["bash", str(ROOT / "restore-connector.sh")],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            commands = log.read_text(encoding="utf-8")

        self.assertIn("set --advertise-exit-node=false", commands)
        self.assertIn("set --advertise-connector", commands)
        self.assertNotIn("up --reset", commands)
        self.assertIn("No tag:ai-egress-* connector tag is visible", result.stderr)

    def test_restore_connector_dry_run_skips_verification_consistently(self):
        for args in (["--dry-run"], ["--dry-run", "--force-reset", "--yes"]):
            with self.subTest(args=args):
                with tempfile.TemporaryDirectory() as tmp:
                    env, _log = self._helper_env(tmp)
                    result = subprocess.run(
                        ["bash", str(ROOT / "restore-connector.sh"), *args],
                        env=env,
                        text=True,
                        capture_output=True,
                        check=True,
                    )

                self.assertIn("Dry run only; restore verification was not run.", result.stdout)
                self.assertNotIn("Expected connector tag", result.stdout + result.stderr)

    def test_restore_connector_force_reset_uses_full_connector_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log = self._helper_env(tmp)
            env["RESTORE_RESET_ACK"] = "1"
            subprocess.run(
                ["bash", str(ROOT / "restore-connector.sh"), "--force-reset"],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            commands = log.read_text(encoding="utf-8")

        self.assertIn("up --reset --hostname=ai-egress-jp-01 --advertise-connector --advertise-exit-node=false --advertise-tags=tag:ai-egress-jp", commands)

    def test_restore_connector_force_reset_detects_dynamic_identity_from_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log = self._helper_env(
                tmp,
                status_json={
                    "Self": {
                        "Online": True,
                        "Tags": ["tag:ai-egress-us"],
                        "HostName": "ai-egress-us-abc",
                        "AllowedIPs": [],
                    }
                },
            )
            env["RESTORE_RESET_ACK"] = "1"
            subprocess.run(
                ["bash", str(ROOT / "restore-connector.sh"), "--force-reset"],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            commands = log.read_text(encoding="utf-8")

        self.assertIn("up --reset --hostname=ai-egress-us-abc --advertise-connector --advertise-exit-node=false --advertise-tags=tag:ai-egress-us", commands)
        self.assertNotIn("ai-egress-jp-01", commands)

    def test_restore_connector_force_reset_uses_persisted_identity_when_status_matches_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            generated = Path(tmp) / "generated"
            generated.mkdir()
            (generated / "connector-identity.env").write_text(
                "REGION=tw\nCONNECTOR_TAG=tag:ai-egress-tw\nCONNECTOR_HOSTNAME=ai-egress-tw-main\n",
                encoding="utf-8",
            )
            env, log = self._helper_env(
                tmp,
                status_json={"Self": {"Online": True, "Tags": [], "HostName": "ai-egress-tw-main"}},
            )
            env["GENERATED_DIR"] = str(generated)
            env["RESTORE_RESET_ACK"] = "1"
            subprocess.run(
                ["bash", str(ROOT / "restore-connector.sh"), "--force-reset"],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            commands = log.read_text(encoding="utf-8")

        self.assertIn("up --reset --hostname=ai-egress-tw-main --advertise-connector --advertise-exit-node=false --advertise-tags=tag:ai-egress-tw", commands)

    def test_restore_connector_force_reset_region_source_does_not_mix_with_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            generated = Path(tmp) / "generated"
            generated.mkdir()
            (generated / "connector-identity.env").write_text(
                "REGION=tw\nCONNECTOR_TAG=tag:ai-egress-tw\nCONNECTOR_HOSTNAME=ai-egress-tw-main\n",
                encoding="utf-8",
            )
            env, log = self._helper_env(
                tmp,
                status_json={
                    "Self": {
                        "Online": True,
                        "Tags": ["tag:ai-egress-us"],
                        "HostName": "ai-egress-us-01",
                    }
                },
            )
            env["GENERATED_DIR"] = str(generated)
            env["REGION"] = "us"
            env["RESTORE_RESET_ACK"] = "1"
            subprocess.run(
                ["bash", str(ROOT / "restore-connector.sh"), "--force-reset"],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            commands = log.read_text(encoding="utf-8")

        self.assertIn("up --reset --hostname=ai-egress-us-01 --advertise-connector --advertise-exit-node=false --advertise-tags=tag:ai-egress-us", commands)
        self.assertNotIn("ai-egress-tw-main", commands)

    def test_restore_connector_force_reset_full_env_pair_overrides_stale_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            generated = Path(tmp) / "generated"
            generated.mkdir()
            (generated / "connector-identity.env").write_text(
                "REGION=tw\nCONNECTOR_TAG=tag:ai-egress-tw\nCONNECTOR_HOSTNAME=ai-egress-tw-main\n",
                encoding="utf-8",
            )
            env, log = self._helper_env(
                tmp,
                status_json={"Self": {"Online": True, "Tags": ["tag:old"], "HostName": "old-host"}},
            )
            env["GENERATED_DIR"] = str(generated)
            env["CONNECTOR_TAG"] = "tag:ai-egress-us"
            env["CONNECTOR_HOSTNAME"] = "ai-egress-us-repair"
            env["RESTORE_RESET_ACK"] = "1"
            result = subprocess.run(
                ["bash", str(ROOT / "restore-connector.sh"), "--force-reset"],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            commands = log.read_text(encoding="utf-8")

        self.assertIn("Using connector identity from environment", result.stdout)
        self.assertIn("up --reset --hostname=ai-egress-us-repair --advertise-connector --advertise-exit-node=false --advertise-tags=tag:ai-egress-us", commands)
        self.assertNotIn("ai-egress-tw-main", commands)

    def test_restore_connector_force_reset_refuses_partial_env_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log = self._helper_env(tmp)
            env["CONNECTOR_TAG"] = "tag:ai-egress-us"
            env["RESTORE_RESET_ACK"] = "1"
            result = subprocess.run(
                ["bash", str(ROOT / "restore-connector.sh"), "--force-reset"],
                env=env,
                text=True,
                capture_output=True,
            )

            commands = log.read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 1)
        self.assertIn("Explicit connector identity is incomplete", result.stderr)
        self.assertNotIn("up --reset", commands)

    def test_restore_connector_force_reset_refuses_ambiguous_status_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log = self._helper_env(
                tmp,
                status_json={
                    "Self": {
                        "Online": True,
                        "Tags": ["tag:ai-egress-us", "tag:ai-egress-tw"],
                        "HostName": "ai-egress-us-abc",
                    }
                },
            )
            env["RESTORE_RESET_ACK"] = "1"
            result = subprocess.run(
                ["bash", str(ROOT / "restore-connector.sh"), "--force-reset"],
                env=env,
                text=True,
                capture_output=True,
            )

            commands = log.read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 1)
        self.assertIn("multiple tag:ai-egress-* tags", result.stderr)
        self.assertNotIn("up --reset", commands)

    def test_restore_connector_force_reset_refuses_stale_persisted_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            generated = Path(tmp) / "generated"
            generated.mkdir()
            (generated / "connector-identity.env").write_text(
                "REGION=tw\nCONNECTOR_TAG=tag:ai-egress-tw\nCONNECTOR_HOSTNAME=ai-egress-tw-main\n",
                encoding="utf-8",
            )
            env, log = self._helper_env(
                tmp,
                status_json={"Self": {"Online": True, "Tags": [], "HostName": "replacement-host"}},
            )
            env["GENERATED_DIR"] = str(generated)
            env["RESTORE_RESET_ACK"] = "1"
            result = subprocess.run(
                ["bash", str(ROOT / "restore-connector.sh"), "--force-reset"],
                env=env,
                text=True,
                capture_output=True,
            )

            commands = log.read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 1)
        self.assertIn("conflicts with current Tailscale hostname", result.stderr)
        self.assertNotIn("up --reset", commands)

    def test_restore_connector_force_reset_refuses_malformed_status_with_persisted_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            generated = Path(tmp) / "generated"
            generated.mkdir()
            (generated / "connector-identity.env").write_text(
                "REGION=jp\nCONNECTOR_TAG=tag:ai-egress-jp\nCONNECTOR_HOSTNAME=ai-egress-jp-01\n",
                encoding="utf-8",
            )
            env, log = self._helper_env(tmp)
            (Path(tmp) / "status.json").write_text("{not valid json", encoding="utf-8")
            env["GENERATED_DIR"] = str(generated)
            env["RESTORE_RESET_ACK"] = "1"
            result = subprocess.run(
                ["bash", str(ROOT / "restore-connector.sh"), "--force-reset"],
                env=env,
                text=True,
                capture_output=True,
            )

            commands = log.read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 1)
        self.assertIn("Could not parse Tailscale status JSON", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("up --reset", commands)

    def test_restore_connector_default_warns_on_malformed_verification_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log = self._helper_env(tmp)
            (Path(tmp) / "status.json").write_text("{not valid json", encoding="utf-8")
            result = subprocess.run(
                ["bash", str(ROOT / "restore-connector.sh")],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            commands = log.read_text(encoding="utf-8")

        self.assertIn("set --advertise-exit-node=false", commands)
        self.assertIn("set --advertise-connector", commands)
        self.assertIn("verification was skipped", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_restore_connector_force_reset_refuses_without_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, log = self._helper_env(tmp, status_json={"Self": {"Online": True, "Tags": [], "AllowedIPs": []}})
            env["RESTORE_RESET_ACK"] = "1"
            result = subprocess.run(
                ["bash", str(ROOT / "restore-connector.sh"), "--force-reset"],
                env=env,
                text=True,
                capture_output=True,
            )

            commands = log.read_text(encoding="utf-8")

        self.assertEqual(result.returncode, 1)
        self.assertIn("Could not identify a connector tag", result.stderr)
        self.assertNotIn("up --reset", commands)

    def test_standalone_pack_domains_match_policy_files(self):
        for pack, file_name in [
            ("common", "default-ai-domains.json"),
        ]:
            with self.subTest(pack=pack):
                embedded = subprocess.run(
                    [
                        "bash",
                        "-c",
                        f"AI_EGRESS_SOURCE_ONLY=1; source '{ROOT / 'check-client-routes.sh'}' && pack_domains {pack}",
                    ],
                    text=True,
                    capture_output=True,
                    check=True,
                ).stdout.splitlines()
                policy = subprocess.run(
                    [
                        "python3",
                        str(ROOT / "scripts/policy_tool.py"),
                        "domains",
                        "--domains-file",
                        str(ROOT / "policy" / file_name),
                    ],
                    text=True,
                    capture_output=True,
                    check=True,
                ).stdout.splitlines()
                self.assertEqual(embedded, policy)

    def _client_route_env(self, tmp, *, os_name="Darwin", status_json=None, partial=False, exit_node=False, linux_userspace=False):
        fake_bin = Path(tmp) / "bin"
        fake_bin.mkdir()

        if status_json is None:
            if exit_node:
                status_json = '{"Self":{"Online":true,"TailscaleIPs":["100.64.0.1"]},"Peer":{"p":{"TailscaleIPs":["100.64.0.2"]}},"ExitNodeStatus":{"ID":"nodeid","Online":true}}'
            elif linux_userspace:
                status_json = '{"Self":{"Online":true,"TailscaleIPs":["100.64.0.1"]},"Peer":{},"ExitNodeStatus":null}'
            else:
                status_json = '{"Self":{"Online":true,"TailscaleIPs":["100.64.0.1"]},"Peer":{"p":{"TailscaleIPs":["100.64.0.2"]}},"ExitNodeStatus":null}'

        status_file = Path(tmp) / "status.json"
        status_file.write_text(status_json, encoding="utf-8")

        self._write_fake_command(
            fake_bin,
            "tailscale",
            f"""#!/bin/sh
if [ "$1" = "status" ] && [ "$2" = "--json" ]; then
  cat {status_file}
  exit 0
fi
if [ "$1" = "version" ]; then
  echo "1.80.0-test"
  exit 0
fi
exit 0
""",
        )
        self._write_fake_command(fake_bin, "uname", f"#!/bin/sh\necho {os_name}\n")
        self._write_fake_command(
            fake_bin,
            "dig",
            """#!/bin/sh
domain="$2"
case "$domain" in
  chatgpt.com)
    echo 104.18.1.1
    echo 104.18.1.2
    ;;
  ipinfo.io)
    echo 34.117.59.81
    ;;
esac
""",
        )

        if os_name == "Darwin":
            second_iface = "en0" if partial else "utun8"
            baseline_iface = "utun8" if exit_node else "en0"
            self._write_fake_command(
                fake_bin,
                "route",
                f"""#!/bin/sh
eval "target=\\${{$#}}"
case "$target" in
  100.64.0.2|104.18.1.1) iface=utun8 ;;
  104.18.1.2) iface={second_iface} ;;
  34.117.59.81) iface={baseline_iface} ;;
  *) iface=en0 ;;
esac
echo "interface: $iface"
""",
            )
            self._write_fake_command(fake_bin, "ifconfig", "#!/bin/sh\nexit 0\n")
        else:
            self._write_fake_command(
                fake_bin,
                "ip",
                """#!/bin/sh
if [ "$1" = "link" ]; then
  exit 1
fi
if [ "$1" = "route" ]; then
  eval "target=\\${{$#}}"
  echo "$target via 192.0.2.1 dev eth0"
  exit 0
fi
exit 1
""",
            )

        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
        return env

    def test_client_route_checker_darwin_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            domains = Path(tmp) / "domains.txt"
            domains.write_text("chatgpt.com\n", encoding="utf-8")
            env = self._client_route_env(tmp)
            result = subprocess.run(
                ["bash", str(ROOT / "check-client-routes.sh"), "--domains-file", str(domains)],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

        self.assertIn("chatgpt.com -> 104.18.1.1 -> utun8 via Tailscale", result.stdout)
        self.assertIn("ipinfo.io -> 34.117.59.81 -> en0 normal traffic local", result.stdout)

    def test_client_route_checker_partial_routes_warn_without_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            domains = Path(tmp) / "domains.txt"
            domains.write_text("chatgpt.com\n", encoding="utf-8")
            env = self._client_route_env(tmp, partial=True)
            result = subprocess.run(
                ["bash", str(ROOT / "check-client-routes.sh"), "--domains-file", str(domains)],
                env=env,
                text=True,
                capture_output=True,
            )

        self.assertEqual(result.returncode, 0)
        self.assertIn("partial route coverage", result.stdout)
        self.assertIn("[WARN] chatgpt.com -> 104.18.1.2 -> en0 is not yet routed through Tailscale", result.stdout)

    def test_client_route_checker_exit_node_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            domains = Path(tmp) / "domains.txt"
            domains.write_text("chatgpt.com\n", encoding="utf-8")
            env = self._client_route_env(tmp, exit_node=True)
            result = subprocess.run(
                ["bash", str(ROOT / "check-client-routes.sh"), "--domains-file", str(domains)],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

        self.assertIn("Exit node appears active", result.stdout)
        self.assertIn("ipinfo.io -> 34.117.59.81 -> utun8 via selected exit node; expected because full-traffic exit-node mode is active", result.stdout)

    def test_client_route_checker_json_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            domains = Path(tmp) / "domains.txt"
            domains.write_text("chatgpt.com\n", encoding="utf-8")
            env = self._client_route_env(tmp)
            result = subprocess.run(
                ["bash", str(ROOT / "check-client-routes.sh"), "--domains-file", str(domains), "--json"],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

        payload = json.loads(result.stdout)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["script"], "check-client-routes.sh")
        self.assertIn("summary", payload)
        self.assertTrue(any(check["id"] == "ai-domain-route" for check in payload["checks"]))

    def test_client_route_checker_json_classifies_ai_dns_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            domains = Path(tmp) / "domains.txt"
            domains.write_text("unresolved.example\n", encoding="utf-8")
            env = self._client_route_env(tmp)
            result = subprocess.run(
                ["bash", str(ROOT / "check-client-routes.sh"), "--domains-file", str(domains), "--json"],
                env=env,
                text=True,
                capture_output=True,
            )

        self.assertEqual(result.returncode, 1)
        checks = json.loads(result.stdout)["checks"]
        self.assertTrue(
            any(
                check["id"] == "ai-domain-route"
                and check["status"] == "fail"
                and "could not resolve" in check["message"]
                for check in checks
            )
        )

    def test_client_route_checker_json_classifies_baseline_dns_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            domains = Path(tmp) / "domains.txt"
            domains.write_text("chatgpt.com\n", encoding="utf-8")
            env = self._client_route_env(tmp)
            result = subprocess.run(
                [
                    "bash",
                    str(ROOT / "check-client-routes.sh"),
                    "--domains-file",
                    str(domains),
                    "--baseline-domain",
                    "unresolved.example",
                    "--json",
                ],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

        checks = json.loads(result.stdout)["checks"]
        self.assertTrue(
            any(
                check["id"] == "baseline-route"
                and check["status"] == "warn"
                and "baseline could not resolve" in check["message"]
                for check in checks
            )
        )

    def test_client_route_checker_linux_userspace_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            domains = Path(tmp) / "domains.txt"
            domains.write_text("chatgpt.com\n", encoding="utf-8")
            env = self._client_route_env(tmp, os_name="Linux", linux_userspace=True)
            result = subprocess.run(
                ["bash", str(ROOT / "check-client-routes.sh"), "--domains-file", str(domains)],
                env=env,
                text=True,
                capture_output=True,
            )

        self.assertEqual(result.returncode, 0)
        self.assertIn("userspace networking may hide the actual path", result.stdout)
        self.assertIn("Linux userspace networking may hide the path", result.stdout)

    def test_client_route_checker_tailscale_not_running_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp) / "bin"
            fake_bin.mkdir()
            self._write_fake_command(
                fake_bin,
                "tailscale",
                """#!/bin/sh
if [ "$1" = "status" ]; then exit 1; fi
if [ "$1" = "version" ]; then echo "1.80.0-test"; exit 0; fi
exit 1
""",
            )
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
            result = subprocess.run(
                ["bash", str(ROOT / "check-client-routes.sh"), "--domain-pack", "common"],
                env=env,
                text=True,
                capture_output=True,
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("Tailscale is not running or this client is not logged in", result.stdout)

    def test_client_route_checker_warns_and_fails_all_wildcard_domains(self):
        with tempfile.TemporaryDirectory() as tmp:
            domains = Path(tmp) / "domains.txt"
            domains.write_text("*.chatgpt.com\n", encoding="utf-8")
            env = self._client_route_env(tmp)
            result = subprocess.run(
                ["bash", str(ROOT / "check-client-routes.sh"), "--domains-file", str(domains), "--json"],
                env=env,
                text=True,
                capture_output=True,
            )

        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        checks = [check for check in payload["checks"] if check["id"] == "ai-domain-route"]
        self.assertTrue(any(check["details"].get("wildcards_skipped") == 1 for check in checks))
        self.assertTrue(any(check["status"] == "fail" and "No non-wildcard" in check["message"] for check in checks))

    def _diagnose_env(self, tmp, *, status_json=None, status_text=None, status_available=True, linux_userspace=False):
        fake_bin = Path(tmp) / "bin"
        fake_bin.mkdir()
        if status_json is None:
            status_json = {
                "Self": {
                    "Online": True,
                    "Tags": ["tag:ai-egress-jp"],
                    "HostName": "ai-egress-jp-01",
                    "AllowedIPs": ["0.0.0.0/0", "::/0"],
                }
            }
        if status_text is None:
            status_text = "100.64.0.1 ai-egress-jp-01 tag:ai-egress-jp offers exit node"

        status_file = Path(tmp) / "status.json"
        status_file.write_text(json.dumps(status_json), encoding="utf-8")
        status_text_file = Path(tmp) / "status.txt"
        status_text_file.write_text(status_text, encoding="utf-8")
        status_json_block = f"""if [ "$1" = "status" ] && [ "$2" = "--json" ]; then
  cat {status_file}
  exit 0
fi
if [ "$1" = "status" ]; then
  cat {status_text_file}
  exit 0
fi
"""
        if not status_available:
            status_json_block = """if [ "$1" = "status" ]; then
  exit 1
fi
"""

        self._write_fake_command(
            fake_bin,
            "tailscale",
            f"""#!/bin/sh
{status_json_block}
if [ "$1" = "version" ]; then
  echo "1.80.0-test"
  exit 0
fi
exit 0
""",
        )
        self._write_fake_command(
            fake_bin,
            "curl",
            """#!/bin/sh
case "$*" in
  *ifconfig.co/asn*) echo AS64500 ;;
  *ifconfig.co/ip*) echo 203.0.113.10 ;;
esac
""",
        )
        self._write_fake_command(
            fake_bin,
            "sysctl",
            """#!/bin/sh
if [ "$1" = "-n" ]; then
  case "$2" in
    net.ipv4.ip_forward|net.ipv6.conf.all.forwarding) echo 1 ;;
  esac
  exit 0
fi
exit 0
""",
        )
        self._write_fake_command(
            fake_bin,
            "dig",
            """#!/bin/sh
echo 104.18.1.1
""",
        )
        self._write_fake_command(
            fake_bin,
            "ip",
            f"""#!/bin/sh
if [ "$1" = "link" ]; then
  {"echo '1: lo: <LOOPBACK>'; exit 0" if linux_userspace else "echo '2: tailscale0: <POINTOPOINT>'; exit 0"}
fi
if [ "$1" = "route" ]; then
  eval "target=\\${{$#}}"
  echo "$target via 100.64.0.1 dev tailscale0"
  exit 0
fi
exit 0
""",
        )
        self._write_fake_command(fake_bin, "uname", "#!/bin/sh\necho Linux\n")

        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
        return env

    def test_diagnose_json_reports_connector_exit_node_and_forwarding(self):
        with tempfile.TemporaryDirectory() as tmp:
            domains = Path(tmp) / "domains.txt"
            domains.write_text("chatgpt.com\n", encoding="utf-8")
            env = self._diagnose_env(tmp)
            result = subprocess.run(
                ["bash", str(ROOT / "diagnose.sh"), "--domains-file", str(domains), "--json"],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

        payload = json.loads(result.stdout)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["script"], "diagnose.sh")
        ids = {check["id"] for check in payload["checks"]}
        self.assertIn("connector-advertised", ids)
        self.assertIn("exit-node-advertised", ids)
        self.assertIn("forwarding", ids)

    def test_diagnose_json_reports_connector_only_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            domains = Path(tmp) / "domains.txt"
            domains.write_text("chatgpt.com\n", encoding="utf-8")
            env = self._diagnose_env(
                tmp,
                status_json={"Self": {"Online": True, "Tags": ["tag:ai-egress-jp"], "AllowedIPs": []}},
                status_text="100.64.0.1 ai-egress-jp-01 tag:ai-egress-jp",
            )
            result = subprocess.run(
                ["bash", str(ROOT / "diagnose.sh"), "--domains-file", str(domains), "--json"],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

        checks = {check["id"]: check for check in json.loads(result.stdout)["checks"]}
        self.assertEqual(checks["connector-advertised"]["details"]["advertised"], True)
        self.assertEqual(checks["exit-node-advertised"]["details"]["advertised"], False)

    def test_diagnose_json_reports_exit_node_only_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            domains = Path(tmp) / "domains.txt"
            domains.write_text("chatgpt.com\n", encoding="utf-8")
            env = self._diagnose_env(
                tmp,
                status_json={"Self": {"Online": True, "Tags": [], "AllowedIPs": ["0.0.0.0/0", "::/0"]}},
                status_text="100.64.0.1 ai-egress-jp-01 offers exit node",
            )
            result = subprocess.run(
                ["bash", str(ROOT / "diagnose.sh"), "--domains-file", str(domains), "--json"],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

        checks = {check["id"]: check for check in json.loads(result.stdout)["checks"]}
        self.assertEqual(checks["connector-advertised"]["status"], "warn")
        self.assertEqual(checks["exit-node-advertised"]["details"]["advertised"], True)

    def test_diagnose_json_reports_unknown_tailscale_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            domains = Path(tmp) / "domains.txt"
            domains.write_text("chatgpt.com\n", encoding="utf-8")
            env = self._diagnose_env(tmp, status_available=False)
            result = subprocess.run(
                ["bash", str(ROOT / "diagnose.sh"), "--domains-file", str(domains), "--json"],
                env=env,
                text=True,
                capture_output=True,
            )

        self.assertEqual(result.returncode, 1)
        checks = {check["id"]: check for check in json.loads(result.stdout)["checks"]}
        self.assertEqual(checks["tailscale-status"]["status"], "fail")
        self.assertEqual(checks["connector-advertised"]["details"]["advertised"], None)
        self.assertEqual(checks["exit-node-advertised"]["details"]["advertised"], None)

    def test_diagnose_json_reports_userspace_networking_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            domains = Path(tmp) / "domains.txt"
            domains.write_text("chatgpt.com\n", encoding="utf-8")
            env = self._diagnose_env(tmp, linux_userspace=True)
            result = subprocess.run(
                ["bash", str(ROOT / "diagnose.sh"), "--domains-file", str(domains), "--json"],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

        checks = [check for check in json.loads(result.stdout)["checks"] if check["id"] == "tailscale-interface"]
        self.assertEqual(checks[0]["status"], "warn")
        self.assertEqual(checks[0]["details"]["userspace_networking"], True)

    def test_diagnose_text_output_includes_sections_and_status_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            domains = Path(tmp) / "domains.txt"
            domains.write_text("chatgpt.com\n", encoding="utf-8")
            env = self._diagnose_env(tmp)
            result = subprocess.run(
                ["bash", str(ROOT / "diagnose.sh"), "--domains-file", str(domains)],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

        self.assertIn("== VPS-side diagnostics ==", result.stdout)
        self.assertIn("== Tailscale ==", result.stdout)
        self.assertIn("[OK] Tailscale status is available.", result.stdout)
        self.assertIn("[OK] Connector advertised: expected ai-egress tag is present.", result.stdout)

    def test_diagnose_fails_when_tailscale_not_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_bin = Path(tmp) / "bin"
            fake_bin.mkdir()
            self._write_fake_command(
                fake_bin,
                "curl",
                """#!/bin/sh
case "$*" in
  *ifconfig.co/asn*) echo AS64500 ;;
  *ifconfig.co/ip*) echo 203.0.113.10 ;;
esac
""",
            )
            self._write_fake_command(
                fake_bin,
                "sysctl",
                """#!/bin/sh
if [ "$1" = "-n" ]; then
  case "$2" in
    net.ipv4.ip_forward|net.ipv6.conf.all.forwarding) echo 1 ;;
  esac
  exit 0
fi
exit 0
""",
            )
            self._write_fake_command(fake_bin, "dig", "#!/bin/sh\necho 104.18.1.1\n")
            self._write_fake_command(fake_bin, "ip", "#!/bin/sh\necho '2: tailscale0: <POINTOPOINT>'\n")
            self._write_fake_command(fake_bin, "uname", "#!/bin/sh\necho Linux\n")
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}{os.pathsep}/usr/bin:/bin:/usr/sbin:/sbin"
            result = subprocess.run(
                ["bash", str(ROOT / "diagnose.sh"), "--domain-pack", "common"],
                env=env,
                text=True,
                capture_output=True,
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("[FAIL] tailscale is not installed.", result.stdout)

    def test_diagnose_warns_and_fails_all_wildcard_sample_domains(self):
        with tempfile.TemporaryDirectory() as tmp:
            domains = Path(tmp) / "domains.txt"
            domains.write_text("*.chatgpt.com\n", encoding="utf-8")
            env = self._diagnose_env(tmp)
            result = subprocess.run(
                ["bash", str(ROOT / "diagnose.sh"), "--domains-file", str(domains), "--json"],
                env=env,
                text=True,
                capture_output=True,
            )

        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        checks = [check for check in payload["checks"] if check["id"] == "sample-domain-routes"]
        self.assertTrue(any(check["details"].get("wildcards_skipped") == 1 for check in checks))
        self.assertTrue(any(check["status"] == "fail" and "No non-wildcard" in check["message"] for check in checks))


if __name__ == "__main__":
    unittest.main()
