import os
import hashlib
import json
import re
import select
import shutil
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


# Frozen copy of the pre-migration inline run_root, used to parity-test the
# extracted ai_egress_run_root in scripts/lib/common.sh.
_ORIGINAL_RUN_ROOT = (
    'run_root() {\n'
    '  if [ "$DRY_RUN" = "1" ]; then\n'
    "    printf '+'\n"
    '    if [ "$(id -u)" -ne 0 ] && [ "$USE_SUDO" != "0" ]; then\n'
    "      printf ' sudo'\n"
    '    fi\n'
    "    printf ' %q' \"$@\"\n"
    "    printf '\\n'\n"
    '    return 0\n'
    '  fi\n'
    '  if [ "$(id -u)" -eq 0 ] || [ "$USE_SUDO" = "0" ]; then\n'
    '    "$@"\n'
    '  else\n'
    '    sudo "$@"\n'
    '  fi\n'
    '}'
)


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
            "scripts/lib/common.sh",
        ]:
            with self.subTest(script=script):
                subprocess.run(["bash", "-n", str(ROOT / script)], check=True)

    @staticmethod
    def _openrc_code_lines(text):
        """Return the non-comment, inline-comment-stripped code lines of an
        openrc-run script (so a comment mentioning `--apply` cannot mask a mutated
        `command_args`, and exact assignment values can be asserted)."""
        lines = []
        for raw in text.splitlines():
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            stripped = stripped.split("#", 1)[0].strip()  # no '#' occurs inside these values
            if stripped:
                lines.append(stripped)
        return lines

    def test_openrc_examples_are_valid(self):
        openrc = ROOT / "docs" / "examples" / "openrc"
        specs = {
            "failover-exit-node": {"command_args": "--watch --apply", "respawn_delay": "10"},
            "monitor-connectors": {"command_args": "--watch", "respawn_delay": "30"},
        }
        for svc, spec in specs.items():
            path = openrc / svc
            with self.subTest(service=svc):
                self.assertTrue(path.exists(), f"{svc} missing")
                self.assertEqual(path.stat().st_mode & 0o111, 0o111, f"{svc} must be executable")
                text = path.read_text(encoding="utf-8")
                self.assertEqual(text.splitlines()[0], "#!/sbin/openrc-run")
                syntax = subprocess.run(["sh", "-n", str(path)], text=True, capture_output=True)
                self.assertEqual(syntax.returncode, 0, syntax.stderr)

                code = self._openrc_code_lines(text)
                self.assertIn(f'command="/opt/tailscale-ai-egress/{svc}.sh"', code)
                # Exact command_args (list membership, not substring): a controller
                # mutated to "--watch" no longer matches "--watch --apply".
                self.assertIn(f'command_args="{spec["command_args"]}"', code)
                self.assertIn('supervisor="supervise-daemon"', code)
                self.assertIn(f"respawn_delay={spec['respawn_delay']}", code)
                # respawn_max=0 (unlimited); removing it restores OpenRC's default of 10.
                self.assertIn("respawn_max=0", code)
                self.assertIn('directory="/opt/tailscale-ai-egress"', code)
                self.assertIn('pidfile="/run/${RC_SVCNAME}.pid"', code)
                # Crash-loop safety fields must survive edits with their real values
                # (an empty output_log= or a dropped log line must fail this test).
                self.assertIn(
                    f'required_files="/opt/tailscale-ai-egress/{svc}.sh '
                    f'/opt/tailscale-ai-egress/scripts/health_check.py"',
                    code,
                )
                self.assertIn('output_log="/var/log/${RC_SVCNAME}.log"', code)
                self.assertIn('error_log="/var/log/${RC_SVCNAME}.log"', code)
                self.assertIn("need net", code)
                self.assertIn("need tailscale", code)
                body = "\n".join(code)
                self.assertIn("start_pre()", body)
                # The preflight must actually check the deps and fail loudly, not just
                # loop over them: a no-op `for _dep in bash python3; do :; done` must fail.
                self.assertRegex(body, r"for\s+\w+\s+in\s+bash\s+python3\b")
                self.assertIn("command -v", body)
                self.assertIn("eerror", body)
                self.assertIn("return 1", body)

        for confd in ("failover-exit-node.confd", "monitor-connectors.confd"):
            path = openrc / confd
            with self.subTest(confd=confd):
                self.assertTrue(path.exists(), f"{confd} missing")
                self.assertEqual(path.stat().st_mode & 0o111, 0, f"{confd} must not be executable")

    def test_common_lib_rejects_direct_execution(self):
        # scripts/lib/common.sh is a source-only library; running it directly must
        # fail with a clear message rather than silently doing nothing.
        result = subprocess.run(
            ["bash", str(ROOT / "scripts/lib/common.sh")],
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("source-only", result.stderr)

    def test_common_lib_sources_without_side_effects(self):
        # Sourcing the skeleton must be a no-op: no output, clean exit. This keeps
        # it safe to source before any helper bodies are added.
        result = subprocess.run(
            ["bash", "-c", f'source "{ROOT / "scripts/lib/common.sh"}"'],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_ai_egress_run_root_matches_original_dry_run(self):
        # Parity: the extracted ai_egress_run_root must produce byte-identical
        # dry-run output to the original inline run_root across
        # (root/non-root) x (USE_SUDO 0/1), including %q quoting of arguments.
        common = ROOT / "scripts/lib/common.sh"
        args = 'tailscale set --exit-node="a b"'
        for uid in ("0", "1000"):
            for use_sudo in ("0", "1"):
                with self.subTest(uid=uid, use_sudo=use_sudo):
                    preamble = f'id() {{ echo "{uid}"; }}\nDRY_RUN=1\nUSE_SUDO={use_sudo}\n'
                    orig = subprocess.run(
                        ["bash", "-c", f"{preamble}{_ORIGINAL_RUN_ROOT}\nrun_root {args}"],
                        text=True, capture_output=True,
                    )
                    new = subprocess.run(
                        ["bash", "-c", f'{preamble}source "{common}"\nai_egress_run_root {args}'],
                        text=True, capture_output=True,
                    )
                    self.assertEqual(orig.returncode, 0)
                    self.assertEqual(new.returncode, 0)
                    self.assertEqual(new.stdout, orig.stdout)

    def test_ai_egress_run_root_matches_original_exec_dispatch(self):
        # Parity for the EXECUTION branch (DRY_RUN=0): direct vs sudo dispatch must
        # match the original across (root/non-root) x (USE_SUDO 0/1). A fake `sudo`
        # marks escalation; a fake `id` sets the uid; the command echoes a marker.
        common = ROOT / "scripts/lib/common.sh"
        cmd = "printf 'CMD\\n'"
        for uid in ("0", "1000"):
            for use_sudo in ("0", "1"):
                with self.subTest(uid=uid, use_sudo=use_sudo):
                    preamble = (
                        f'id() {{ echo "{uid}"; }}\n'
                        'sudo() { printf "SUDO "; "$@"; }\n'
                        f'DRY_RUN=0\nUSE_SUDO={use_sudo}\n'
                    )
                    orig = subprocess.run(
                        ["bash", "-c", f"{preamble}{_ORIGINAL_RUN_ROOT}\nrun_root {cmd}"],
                        text=True, capture_output=True,
                    )
                    new = subprocess.run(
                        ["bash", "-c", f'{preamble}source "{common}"\nai_egress_run_root {cmd}'],
                        text=True, capture_output=True,
                    )
                    self.assertEqual(orig.returncode, 0)
                    self.assertEqual(new.returncode, 0)
                    self.assertEqual(new.stdout, orig.stdout)
                    # Sanity: escalation happens iff non-root and USE_SUDO!=0.
                    expected = "SUDO CMD\n" if (uid != "0" and use_sudo != "0") else "CMD\n"
                    self.assertEqual(new.stdout, expected)

    def test_enable_exit_node_fails_clearly_without_common_lib(self):
        # A checkout missing scripts/lib/common.sh must fail with a clear message,
        # not a cryptic `source: No such file` error.
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "enable-exit-node.sh"
            script.write_text((ROOT / "enable-exit-node.sh").read_text(encoding="utf-8"), encoding="utf-8")
            result = subprocess.run(["bash", str(script), "--dry-run"], text=True, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing shared library", result.stderr)

    def test_migrated_consumers_use_shared_run_root(self):
        # Structural guard for the shared-lib migration: each migrated consumer
        # must source common.sh, no longer define an inline run_root(), call
        # ai_egress_run_root at least as many times as it used to, and leave NO
        # bare `run_root` call token behind (a partial rename). A revert to the
        # inline copy, or a missed call site, fails this.
        min_calls = {"enable-exit-node.sh": 4, "disable-exit-node.sh": 1, "restore-connector.sh": 3}
        for name, expected in min_calls.items():
            with self.subTest(script=name):
                text = (ROOT / name).read_text(encoding="utf-8")
                self.assertIn('. "$COMMON_LIB"', text)
                self.assertNotIn("run_root() {", text)
                self.assertGreaterEqual(text.count("ai_egress_run_root"), expected)
                # No bare `run_root` token: allows ai_egress_run_root / run_root_output.
                leftover = re.search(r"(?<![A-Za-z_])run_root(?![A-Za-z_(])", text)
                self.assertIsNone(leftover, f"leftover bare run_root token in {name}")

        # restore-connector.sh must PRESERVE its separate run_root_output helper.
        restore = (ROOT / "restore-connector.sh").read_text(encoding="utf-8")
        self.assertIn("run_root_output() {", restore)
        self.assertIn("run_root_output tailscale", restore)
        self.assertNotIn("ai_egress_run_root_output", restore)

    def test_run_root_output_dispatch_unchanged(self):
        # run_root_output (restore-only; deferred from the shared-lib migration)
        # must keep its behavior: direct/sudo dispatch by (root/USE_SUDO) and
        # crucially NO dry-run branch. Extract the REAL function from the script
        # and exercise it; a body change that altered dispatch or added a dry-run
        # branch fails this.
        src = (ROOT / "restore-connector.sh").read_text(encoding="utf-8")
        match = re.search(r"^run_root_output\(\) \{.*?^\}", src, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(match, "run_root_output definition not found")
        func = match.group(0)
        for uid in ("0", "1000"):
            for use_sudo in ("0", "1"):
                with self.subTest(uid=uid, use_sudo=use_sudo):
                    # DRY_RUN=1 must be IGNORED (run_root_output has no dry-run branch).
                    preamble = (
                        f'id() {{ echo "{uid}"; }}\n'
                        'sudo() { printf "SUDO "; "$@"; }\n'
                        f'USE_SUDO={use_sudo}\nDRY_RUN=1\n'
                    )
                    out = subprocess.run(
                        ["bash", "-c", f"{preamble}{func}\nrun_root_output printf 'CMD\\n'"],
                        text=True, capture_output=True,
                    )
                    self.assertEqual(out.returncode, 0)
                    expected = "SUDO CMD\n" if (uid != "0" and use_sudo != "0") else "CMD\n"
                    self.assertEqual(out.stdout, expected)

    def test_disable_exit_node_fails_clearly_without_common_lib(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "disable-exit-node.sh"
            script.write_text((ROOT / "disable-exit-node.sh").read_text(encoding="utf-8"), encoding="utf-8")
            result = subprocess.run(["bash", str(script), "--dry-run"], text=True, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing shared library", result.stderr)

    def test_restore_connector_fails_clearly_without_common_lib(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "restore-connector.sh"
            script.write_text((ROOT / "restore-connector.sh").read_text(encoding="utf-8"), encoding="utf-8")
            result = subprocess.run(["bash", str(script), "--dry-run"], text=True, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing shared library", result.stderr)

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

    def _run_install(self, tmp, fx, *, args=("--dry-run",), check=None, **overrides):
        """Run install.sh in cwd=tmp with the fake bin on PATH; return (result, calls).
        `calls` is the parsed fake-gh call log (empty if gh was never invoked)."""
        calls_log = Path(tmp) / "gh-calls.log"
        env = self._install_env(fx["fake_bin"], **overrides)
        result = subprocess.run(
            ["bash", str(ROOT / "install.sh"), *args],
            cwd=tmp,
            env=env,
            text=True,
            capture_output=True,
            check=bool(check),
        )
        _, logged = self._capture_log(calls_log)
        return result, self._parse_gh_calls(logged)

    def test_install_wrapper_downloads_and_verifies_release_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            fx = self._make_release_fixture(Path(tmp))
            self._write_fake_gh(
                fx["fake_bin"], version="2.93.0", calls_log=Path(tmp) / "gh-calls.log"
            )
            result, calls = self._run_install(tmp, fx, check=True)

        out = result.stdout
        version = fx["version"]
        asset_name = fx["asset_name"]
        self.assertIn(
            f"Downloading https://github.com/F-e-u-e-r/tailscale-ai-egress/releases/download/v{version}/{asset_name}",
            out,
        )
        self.assertIn(f"{asset_name}: OK", out)
        self.assertIn("fake bootstrap --dry-run", out)
        # Exactly one verify with the EXACT hardened argv + telemetry opt-out.
        self._assert_single_verify(
            calls, slug="F-e-u-e-r/tailscale-ai-egress", version=version, asset_name=asset_name
        )
        # Every gh invocation (the version probe too) disables telemetry.
        self.assertTrue(calls)
        for call in calls:
            self.assertEqual(call["telemetry"], "GH_TELEMETRY=false")
        # Order: checksum success -> attestation verify -> extraction/bootstrap.
        self.assertLess(out.index(f"{asset_name}: OK"), out.index("Verifying release attestation"))
        self.assertLess(out.index("Verifying release attestation"), out.index("fake bootstrap"))

    def test_install_attestation_uses_fork_repo_slug(self):
        # A custom fork URL with a trailing slash derives owner/repo (not a
        # hardcoded default) and parses the trailing slash correctly.
        with tempfile.TemporaryDirectory() as tmp:
            fx = self._make_release_fixture(Path(tmp))
            self._write_fake_gh(
                fx["fake_bin"], version="2.95.0", calls_log=Path(tmp) / "gh-calls.log"
            )
            result, calls = self._run_install(
                tmp, fx, check=True,
                TAILSCALE_AI_EGRESS_REPO="https://github.com/custom-owner/custom-repo/",
            )

        self.assertIn("fake bootstrap --dry-run", result.stdout)
        self._assert_single_verify(
            calls, slug="custom-owner/custom-repo", version=fx["version"],
            asset_name=fx["asset_name"],
        )
        flat = " ".join(a for c in calls for a in c["argv"])
        self.assertNotIn("F-e-u-e-r", flat)

    def test_install_attestation_dot_git_repo_slug(self):
        # A `.git`-suffixed fork URL strips the suffix to owner/repo.
        with tempfile.TemporaryDirectory() as tmp:
            fx = self._make_release_fixture(Path(tmp))
            self._write_fake_gh(
                fx["fake_bin"], version="2.93.0", calls_log=Path(tmp) / "gh-calls.log"
            )
            result, calls = self._run_install(
                tmp, fx, check=True,
                TAILSCALE_AI_EGRESS_REPO="https://github.com/custom-owner/custom-repo.git",
            )

        self.assertIn("fake bootstrap --dry-run", result.stdout)
        self._assert_single_verify(
            calls, slug="custom-owner/custom-repo", version=fx["version"],
            asset_name=fx["asset_name"],
        )

    def test_install_attestation_future_major_gh_verifies(self):
        # A future major (>= 3) is accepted -- the version floor is numeric, not lexical.
        with tempfile.TemporaryDirectory() as tmp:
            fx = self._make_release_fixture(Path(tmp))
            self._write_fake_gh(
                fx["fake_bin"], version="3.0.0", calls_log=Path(tmp) / "gh-calls.log"
            )
            _, calls = self._run_install(tmp, fx, check=True)

        self._assert_single_verify(
            calls, slug="F-e-u-e-r/tailscale-ai-egress", version=fx["version"],
            asset_name=fx["asset_name"],
        )

    def test_install_attestation_absent_gh_skips(self):
        # Reliable absence: PATH holds ONLY an isolated dir of symlinked real
        # tools plus the fake curl -- no gh anywhere -- and Bash is invoked by
        # absolute path so it does not depend on PATH resolution. `gzip` is
        # included because GNU `tar -xzf` forks it (macOS bsdtar would hide that).
        sha_tool = shutil.which("sha256sum") or shutil.which("shasum")
        real_bash = shutil.which("bash")
        if sha_tool is None or real_bash is None:
            self.skipTest("sha256sum/shasum or bash not found on PATH")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fx = self._make_release_fixture(tmp_path)
            isolated = tmp_path / "isolated"
            isolated.mkdir()
            for tool in ("sh", "env", "tar", "gzip", "mktemp", "grep", "cat", "cp", "rm", "dirname"):
                real = shutil.which(tool)
                if real is None:
                    self.skipTest(f"required tool {tool!r} not found on PATH")
                os.symlink(real, isolated / tool)
            os.symlink(real_bash, isolated / "bash")
            os.symlink(sha_tool, isolated / Path(sha_tool).name)
            os.symlink(fx["fake_bin"] / "curl", isolated / "curl")

            path = str(isolated)
            self.assertIsNone(shutil.which("gh", path=path))
            env = {"PATH": path}
            for key in ("HOME", "TMPDIR", "LANG", "LC_ALL"):
                if key in os.environ:
                    env[key] = os.environ[key]
            result = subprocess.run(
                [real_bash, str(ROOT / "install.sh"), "--dry-run"],
                cwd=tmp,
                env=env,
                text=True,
                capture_output=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gh not found", result.stdout)
        self.assertIn("fake bootstrap --dry-run", result.stdout)

    def test_install_attestation_old_gh_skips(self):
        # gh present but < 2.93.0 -> skip; the vulnerable verify is never invoked
        # (the version probe still runs, so the log is non-empty but has no verify).
        with tempfile.TemporaryDirectory() as tmp:
            fx = self._make_release_fixture(Path(tmp))
            self._write_fake_gh(
                fx["fake_bin"], version="2.92.0", calls_log=Path(tmp) / "gh-calls.log"
            )
            result, calls = self._run_install(tmp, fx, check=True)

        self.assertIn("older than 2.93.0", result.stdout)
        self.assertEqual([c for c in calls if c["argv"][:2] == ["attestation", "verify"]], [])
        self.assertIn("fake bootstrap --dry-run", result.stdout)

    def test_install_attestation_unparseable_gh_skips(self):
        # A non-numeric gh version (dev build) is unrecognized -> skip, not fail.
        with tempfile.TemporaryDirectory() as tmp:
            fx = self._make_release_fixture(Path(tmp))
            self._write_fake_gh(
                fx["fake_bin"], version="DEV", calls_log=Path(tmp) / "gh-calls.log"
            )
            result, calls = self._run_install(tmp, fx, check=True)

        self.assertIn("older than 2.93.0", result.stdout)
        self.assertEqual([c for c in calls if c["argv"][:2] == ["attestation", "verify"]], [])
        self.assertIn("fake bootstrap --dry-run", result.stdout)

    def test_install_attestation_failed_version_probe_skips(self):
        # `gh --version` exiting non-zero is treated as unusable -> skip, not fail.
        with tempfile.TemporaryDirectory() as tmp:
            fx = self._make_release_fixture(Path(tmp))
            self._write_fake_gh(
                fx["fake_bin"], version="2.93.0", version_exit=1,
                calls_log=Path(tmp) / "gh-calls.log",
            )
            result, calls = self._run_install(tmp, fx, check=True)

        self.assertIn("older than 2.93.0", result.stdout)
        self.assertEqual([c for c in calls if c["argv"][:2] == ["attestation", "verify"]], [])
        self.assertIn("fake bootstrap --dry-run", result.stdout)

    def test_install_attestation_verify_failure_is_fatal(self):
        # A usable gh whose verify FAILS aborts before extraction/bootstrap.
        with tempfile.TemporaryDirectory() as tmp:
            fx = self._make_release_fixture(Path(tmp))
            self._write_fake_gh(
                fx["fake_bin"], version="2.93.0", verify_exit=1,
                calls_log=Path(tmp) / "gh-calls.log",
            )
            result, calls = self._run_install(tmp, fx)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("attestation verification failed", result.stderr)
        self.assertEqual(
            len([c for c in calls if c["argv"][:2] == ["attestation", "verify"]]), 1
        )
        self.assertNotIn("fake bootstrap", result.stdout)

    def test_install_attestation_unauthenticated_gh_degrades(self):
        # gh exit code 4 is its documented "requires authentication" code:
        # `gh attestation verify` needs a github.com credential even for public
        # repos, and a fresh host commonly has gh installed but never logged in.
        # That is inability-to-check, not a rejection -> checksum-only with a
        # loud note, NOT a fatal abort (verify_exit=1 above stays fatal).
        with tempfile.TemporaryDirectory() as tmp:
            fx = self._make_release_fixture(Path(tmp))
            self._write_fake_gh(
                fx["fake_bin"], version="2.94.0", verify_exit=4,
                calls_log=Path(tmp) / "gh-calls.log",
            )
            result, calls = self._run_install(tmp, fx, check=True)

        self.assertIn("gh is not authenticated for github.com", result.stdout)
        self.assertNotIn("attestation verification failed", result.stderr)
        # The verify was attempted exactly once (the auth failure happened inside
        # gh, not via a skipped call), then the install degraded and completed.
        self.assertEqual(
            len([c for c in calls if c["argv"][:2] == ["attestation", "verify"]]), 1
        )
        self.assertIn("fake bootstrap --dry-run", result.stdout)

    def test_install_attestation_opt_out_skips(self):
        # Opt-out short-circuits before ANY gh call (empty call log), even with a
        # usable gh present.
        with tempfile.TemporaryDirectory() as tmp:
            fx = self._make_release_fixture(Path(tmp))
            self._write_fake_gh(
                fx["fake_bin"], version="2.93.0", calls_log=Path(tmp) / "gh-calls.log"
            )
            result, calls = self._run_install(
                tmp, fx, check=True, TAILSCALE_AI_EGRESS_SKIP_ATTESTATION="1"
            )

        self.assertIn("skipped (TAILSCALE_AI_EGRESS_SKIP_ATTESTATION=1)", result.stdout)
        self.assertEqual(calls, [], "gh must not be invoked at all when opted out")
        self.assertIn("fake bootstrap --dry-run", result.stdout)

    def test_install_attestation_opt_out_bypasses_non_github_mirror(self):
        # Opt-out must bypass slug derivation entirely (a non-GitHub mirror URL).
        with tempfile.TemporaryDirectory() as tmp:
            fx = self._make_release_fixture(Path(tmp))
            self._write_fake_gh(
                fx["fake_bin"], version="2.93.0", calls_log=Path(tmp) / "gh-calls.log"
            )
            result, calls = self._run_install(
                tmp, fx, check=True,
                TAILSCALE_AI_EGRESS_REPO="https://gitlab.com/owner/repo",
                TAILSCALE_AI_EGRESS_SKIP_ATTESTATION="1",
            )

        self.assertIn("skipped (TAILSCALE_AI_EGRESS_SKIP_ATTESTATION=1)", result.stdout)
        self.assertNotIn("could not derive", result.stderr)
        self.assertEqual(calls, [], "gh must not be invoked at all when opted out")
        self.assertIn("fake bootstrap --dry-run", result.stdout)

    def test_install_attestation_non_github_url_variants_are_fatal(self):
        # A usable gh + a REPO_URL that is not exactly https://github.com/<o>/<r>
        # (no opt-out) fails closed BEFORE any verify: wrong host, look-alike host,
        # owner-only, and extra path segments must all be rejected.
        for repo_url in (
            "https://gitlab.com/owner/repo",
            "https://github.com.evil/owner/repo",
            "https://github.example.com/owner/repo",
            "https://github.com/owner",
            "https://github.com/owner/repo/tree/main",
        ):
            with self.subTest(repo_url=repo_url), tempfile.TemporaryDirectory() as tmp:
                fx = self._make_release_fixture(Path(tmp))
                self._write_fake_gh(
                    fx["fake_bin"], version="2.93.0", calls_log=Path(tmp) / "gh-calls.log"
                )
                result, calls = self._run_install(
                    tmp, fx, TAILSCALE_AI_EGRESS_REPO=repo_url
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("could not derive", result.stderr)
                self.assertEqual(
                    [c for c in calls if c["argv"][:2] == ["attestation", "verify"]], []
                )
                self.assertNotIn("fake bootstrap", result.stdout)

    def test_install_attestation_branch_path_skips_gh(self):
        # The unverified branch path never touches gh, even when a usable gh exists.
        with tempfile.TemporaryDirectory() as tmp:
            fx = self._make_release_fixture(Path(tmp))
            self._write_fake_gh(
                fx["fake_bin"], version="2.93.0", calls_log=Path(tmp) / "gh-calls.log"
            )
            result, calls = self._run_install(
                tmp, fx, check=True, TAILSCALE_AI_EGRESS_BRANCH="main"
            )

        self.assertIn("downloading unverified branch archive", result.stderr)
        self.assertEqual(calls, [], "branch path must not invoke gh at all")
        self.assertIn("fake bootstrap --dry-run", result.stdout)

    def test_install_attestation_version_gate_units(self):
        # Direct contract test of gh_attestation_supported across the grammar.
        func = self._install_sh_function("gh_attestation_supported")
        cases = {
            "2.93.0": True, "2.93.5": True, "2.94.0": True, "2.100.0": True,
            "3.0.0": True, "10.0.0": True, "2.099.0": True, "2.93.0 (extra)": True,
            "2.92.0": False, "2.9.0": False, "2.08.0": False, "1.99.99": False,
            "2.93": False, "2.93.0.1": False, "2.93.0.": False, "2.93.0..": False,
            "2..93": False, ".2.93": False, "2.93.0-rc.1": False, "2.93.0+local": False,
            "DEV": False, "": False,
        }
        for ver, expected in cases.items():
            with self.subTest(version=ver):
                driver = (
                    f'gh() {{ printf "gh version {ver}\\n"; }}\n'
                    f"{func}\n"
                    "if gh_attestation_supported; then echo SUPPORTED; else echo skip; fi\n"
                )
                r = subprocess.run(["bash", "-c", driver], text=True, capture_output=True)
                self.assertEqual(r.returncode, 0, r.stderr)
                got = "SUPPORTED" in r.stdout
                self.assertEqual(got, expected, f"{ver!r}: out={r.stdout!r} err={r.stderr!r}")

    def test_install_attestation_repo_slug_units(self):
        # Direct contract test of derive_repo_slug: HTTPS-github.com only.
        func = self._install_sh_function("derive_repo_slug")
        cases = {
            "https://github.com/F-e-u-e-r/tailscale-ai-egress": "F-e-u-e-r/tailscale-ai-egress",
            "https://github.com/owner/repo/": "owner/repo",
            "https://github.com/owner/repo.git": "owner/repo",
            "https://github.com/owner/repo.git/": "owner/repo",
            "https://github.com/owner/repo.js": "owner/repo.js",
            "http://github.com/owner/repo": None,
            "git@github.com:owner/repo.git": None,
            "https://gitlab.com/owner/repo": None,
            "https://github.com.evil/owner/repo": None,
            "https://github.example.com/owner/repo": None,
            "https://github.com/owner": None,
            "https://github.com/owner/repo/tree/main": None,
            "https://github.com/owner/": None,
            "https://github.com/": None,
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                driver = (
                    f"{func}\n"
                    f'if out="$(derive_repo_slug "{url}")"; then printf "OK:%s" "$out"; '
                    'else printf REJECT; fi\n'
                )
                r = subprocess.run(["bash", "-c", driver], text=True, capture_output=True)
                if expected is None:
                    self.assertEqual(r.stdout, "REJECT", f"{url}: {r.stdout!r} {r.stderr!r}")
                else:
                    self.assertEqual(r.stdout, f"OK:{expected}", f"{url}: {r.stdout!r} {r.stderr!r}")

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

    def _make_release_fixture(self, tmp_path):
        """Build a fake release tarball + SHA256SUMS and a fake `curl` that
        serves them for the release (-fsSLo) and branch (-fsSL) download forms.
        Returns dict(version, asset_name, asset, sums, fake_bin)."""
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        asset_name = f"tailscale-ai-egress-{version}.tar.gz"
        release_root = tmp_path / "release-src" / f"tailscale-ai-egress-{version}"
        release_root.mkdir(parents=True)
        bootstrap = release_root / "bootstrap.sh"
        bootstrap.write_text(
            "#!/usr/bin/env bash\nprintf 'fake bootstrap %s\\n' \"$*\"\n",
            encoding="utf-8",
        )
        bootstrap.chmod(0o755)
        asset = tmp_path / asset_name
        subprocess.run(
            ["tar", "-czf", str(asset), "-C", str(tmp_path / "release-src"), release_root.name],
            check=True,
        )
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        sums = tmp_path / "SHA256SUMS"
        sums.write_text(f"{digest}  {asset_name}\n", encoding="utf-8")
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        self._write_fake_command(
            fake_bin,
            "curl",
            f"""#!/bin/sh
if [ "$1" = "-fsSLo" ]; then
  out="$2"
  url="$3"
  case "$url" in
    */SHA256SUMS) cp {sums} "$out" ;;
    */{asset_name}) cp {asset} "$out" ;;
    *) echo "unexpected URL: $url" >&2; exit 2 ;;
  esac
elif [ "$1" = "-fsSL" ]; then
  url="$2"
  case "$url" in
    */archive/refs/heads/*.tar.gz) cat {asset} ;;
    *) echo "unexpected URL: $url" >&2; exit 2 ;;
  esac
else
  echo "unexpected curl args: $*" >&2
  exit 2
fi
""",
        )
        return {
            "version": version,
            "asset_name": asset_name,
            "asset": asset,
            "sums": sums,
            "fake_bin": fake_bin,
        }

    def _write_fake_gh(self, fake_bin, *, version="2.93.0", verify_exit=0,
                       version_exit=0, calls_log=None):
        """Write a fake `gh` that logs EVERY invocation to calls_log -- the
        GH_TELEMETRY env value plus each argument with `\\037` (unit-separator)
        boundaries -- then answers `--version` and `attestation verify`. An
        absent/empty calls_log proves gh was never run; the boundaries let a test
        assert the exact argv vector (so `--hostname github.com.evil` or a dropped
        flag fails) and the telemetry opt-out, not just substrings."""
        log_block = ""
        if calls_log is not None:
            log_block = (
                "{ "
                "printf 'ENV\\037GH_TELEMETRY=%s\\n' \"${GH_TELEMETRY-<unset>}\"; "
                "printf 'ARGV'; for a in \"$@\"; do printf '\\037%s' \"$a\"; done; "
                "printf '\\n'; "
                f"}} >> {calls_log}\n"
            )
        self._write_fake_command(
            fake_bin,
            "gh",
            f"""#!/bin/sh
{log_block}if [ "$1" = "--version" ]; then
  printf 'gh version {version} (2026-01-01)\\n'
  printf 'https://github.com/cli/cli/releases/tag/v{version}\\n'
  exit {version_exit}
fi
if [ "$1" = "attestation" ] && [ "$2" = "verify" ]; then
  exit {verify_exit}
fi
echo "unexpected gh args: $*" >&2
exit 3
""",
        )

    def _install_sh_function(self, name):
        """Extract a top-level `name() { ... }` shell function from install.sh so
        a test can source and drive it in isolation (the function closes with a
        `}` at column 0; the internal `}` all live inside `${...}` on their line)."""
        lines = (ROOT / "install.sh").read_text(encoding="utf-8").splitlines()
        start = next(i for i, line in enumerate(lines) if line.startswith(f"{name}()"))
        out = [lines[start]]
        for line in lines[start + 1:]:
            out.append(line)
            if line == "}":
                break
        return "\n".join(out)

    def _parse_gh_calls(self, text):
        """Parse a fake-gh calls log into [{'telemetry': str, 'argv': [str, ...]}]."""
        calls = []
        telemetry = None
        for line in text.splitlines():
            if line.startswith("ENV\x1f"):
                telemetry = line.split("\x1f", 1)[1]
            elif line.startswith("ARGV"):
                calls.append({"telemetry": telemetry, "argv": line.split("\x1f")[1:]})
                telemetry = None
        return calls

    def _assert_single_verify(self, calls, *, slug, version, asset_name):
        """Assert exactly one `attestation verify` with the EXACT hardened argv
        vector and the telemetry opt-out. Returns that call."""
        verifies = [c for c in calls if c["argv"][:2] == ["attestation", "verify"]]
        self.assertEqual(len(verifies), 1, f"expected exactly one verify call: {calls}")
        argv = verifies[0]["argv"]
        self.assertTrue(argv[2].endswith(asset_name), f"tarball arg: {argv[2]}")
        self.assertEqual(
            argv[3:],
            [
                "--repo", slug,
                "--signer-workflow", f"{slug}/.github/workflows/release.yml",
                "--source-ref", f"refs/tags/v{version}",
                "--hostname", "github.com",
            ],
        )
        self.assertEqual(verifies[0]["telemetry"], "GH_TELEMETRY=false")
        return verifies[0]

    def _capture_log(self, path):
        """Snapshot a fake-command log's (exists, text) before the enclosing
        TemporaryDirectory is cleaned up -- assertions run after the `with`."""
        if path.exists():
            return True, path.read_text(encoding="utf-8")
        return False, ""

    def _install_env(self, fake_bin, **overrides):
        """A clean environment for install.sh tests: the installer's env knobs
        cleared, the fake bin prepended to PATH, then explicit overrides."""
        env = os.environ.copy()
        for key in (
            "TAILSCALE_AI_EGRESS_BRANCH",
            "TAILSCALE_AI_EGRESS_VERSION",
            "TAILSCALE_AI_EGRESS_REPO",
            "TAILSCALE_AI_EGRESS_SKIP_ATTESTATION",
        ):
            env.pop(key, None)
        env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
        env.update({key: str(value) for key, value in overrides.items()})
        return env

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

    def _client_route_env(self, tmp, *, os_name="Darwin", status_json=None, partial=False, exit_node=False, linux_userspace=False, ipv6=None, ipv4_ai_local=False):
        # ipv6: None -> domains publish NO AAAA (the IPv6 pass skips cleanly). "routed" ->
        # AAAA routes through Tailscale; "local" -> AAAA routes locally (advisory WARN, never
        # FAIL); "partial" -> mixed AAAA (drives the ai-route-summary-ipv6 finding); "noroute"
        # -> AAAA present but the IPv6 route lookup returns nothing (advisory WARN, never FAIL).
        fake_bin = Path(tmp) / "bin"
        fake_bin.mkdir()
        self._dig_log = Path(tmp) / "dig-queries.log"   # one line per dig invocation (arity check)
        aaaa = {
            "routed": {"chatgpt.com": ["2606:4700::a"], "ipinfo.io": ["2001:4860::b"]},
            "local": {"chatgpt.com": ["2606:4700::c"]},
            "partial": {"chatgpt.com": ["2606:4700::a", "2606:4700::c"]},
            "noroute": {"chatgpt.com": ["2606:4700::d"]},
        }.get(ipv6, {})
        aaaa_cases = "".join(
            f"  {dom})\n" + "".join(f"    echo {ip6}\n" for ip6 in ips) + "    ;;\n"
            for dom, ips in aaaa.items()
        )

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
            f"""#!/bin/sh
# argument-strict: the script calls exactly `dig +short <domain> A|AAAA` (3 args). Log each
# invocation so a test can assert exactly one AAAA query per checker call.
echo "$*" >> {self._dig_log}
{{ [ "$1" = "+short" ] && [ $# -eq 3 ]; }} || {{ echo "dig: expected '+short <domain> A|AAAA', got: $*" >&2; exit 64; }}
domain="$2"
rtype="$3"
case "$rtype" in A|AAAA) ;; *) echo "dig: expected A|AAAA, got: $rtype" >&2; exit 64 ;; esac
if [ "$rtype" = "A" ]; then
  case "$domain" in
    chatgpt.com) echo 104.18.1.1; echo 104.18.1.2 ;;
    ipinfo.io) echo 34.117.59.81 ;;
  esac
else
  case "$domain" in
{aaaa_cases}  esac
fi
""",
        )

        ai_iface = "en0" if ipv4_ai_local else "utun8"
        if os_name == "Darwin":
            second_iface = "en0" if (partial or ipv4_ai_local) else "utun8"
            baseline_iface = "utun8" if exit_node else "en0"
            self._write_fake_command(
                fake_bin,
                "route",
                f"""#!/bin/sh
# argument-strict AND family-strict: `route -n get <ipv4>` (3 args) or
# `route -n get -inet6 <ipv6>` (4 args); an IPv4 arm rejects an IPv6 target and vice versa,
# so dropping the `-inet6` flag fails instead of silently matching.
if [ "$1" = "-n" ] && [ "$2" = "get" ] && [ "$3" = "-inet6" ] && [ $# -eq 4 ]; then
  target="$4"
  case "$target" in *:*) ;; *) echo "route: -inet6 needs an IPv6 target: $*" >&2; exit 64 ;; esac
elif [ "$1" = "-n" ] && [ "$2" = "get" ] && [ $# -eq 3 ]; then
  target="$3"
  case "$target" in *:*) echo "route: IPv6 target needs -inet6: $*" >&2; exit 64 ;; esac
else
  echo "route: expected '-n get <ip>' or '-n get -inet6 <ip6>', got: $*" >&2; exit 64
fi
case "$target" in
  100.64.0.2) iface=utun8 ;;
  104.18.1.1) iface={ai_iface} ;;
  104.18.1.2) iface={second_iface} ;;
  34.117.59.81) iface={baseline_iface} ;;
  2606:4700::a) iface=utun8 ;;
  2606:4700::c) iface=en0 ;;
  2001:4860::b) iface=en0 ;;
  2606:4700::d) exit 0 ;;
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
# `ip link`/`ip -o link` are used for interface detection (exit 1 = no tailscale0 here);
# the route-get forms are argument-strict; anything else is an unexpected invocation (64).
case "$1" in
  link) exit 1 ;;
  -o) [ "$2" = "link" ] && exit 1 || { echo "ip: unexpected: $*" >&2; exit 64; } ;;
esac
if [ "$1" = "route" ] && [ "$2" = "get" ]; then
  [ $# -eq 3 ] || { echo "ip: expected 'route get <ip>', got: $*" >&2; exit 64; }
  case "$3" in *:*) echo "ip: IPv6 target needs -6: $*" >&2; exit 64 ;; esac
  echo "$3 via 192.0.2.1 dev eth0"
  exit 0
fi
if [ "$1" = "-6" ] && [ "$2" = "route" ] && [ "$3" = "get" ]; then
  [ $# -eq 4 ] || { echo "ip: expected '-6 route get <ip6>', got: $*" >&2; exit 64; }
  case "$4" in *:*) ;; *) echo "ip: -6 needs an IPv6 target: $*" >&2; exit 64 ;; esac
  echo "$4 via fe80::1 dev tailscale0"
  exit 0
fi
echo "ip: unexpected invocation: $*" >&2
exit 64
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

    def _run_client_route_json(self, tmp, *, os_name="Darwin", ipv6=None, **env_kwargs):
        domains = Path(tmp) / "domains.txt"
        domains.write_text("chatgpt.com\n", encoding="utf-8")
        env = self._client_route_env(tmp, os_name=os_name, ipv6=ipv6, **env_kwargs)
        result = subprocess.run(
            ["bash", str(ROOT / "check-client-routes.sh"), "--domains-file", str(domains), "--json"],
            env=env, text=True, capture_output=True,
        )
        return result, json.loads(result.stdout)

    @staticmethod
    def _no_ipv6_leak(checks):
        # Every check whose message carries a known IPv6 literal must use a `-ipv6` id, so no
        # IPv6 event can leak under an IPv4 id.
        return all(c["id"].endswith("-ipv6") for c in checks if "2606:4700" in c["message"] or "2001:4860" in c["message"])

    def test_client_route_checker_ipv6_darwin_routed(self):
        # The Darwin `route -n get -inet6` arm runs and emits distinct *-ipv6 ids.
        with tempfile.TemporaryDirectory() as tmp:
            result, payload = self._run_client_route_json(tmp, os_name="Darwin", ipv6="routed")
        checks = payload["checks"]
        self.assertEqual(result.returncode, 0)
        self.assertEqual(payload["schema_version"], 1)   # schema is unchanged (additive)
        ai6 = [c for c in checks if c["id"] == "ai-domain-route-ipv6"]
        self.assertTrue(ai6)
        # status OK (routed via Tailscale) -- locks the `-inet6` flag: dropping it makes the
        # fake reject the IPv6 target, route6 returns empty, and this becomes WARN, not OK.
        self.assertTrue(any(c["status"] == "ok" and "2606:4700" in c["message"] for c in ai6))
        self.assertTrue(any(c["id"] == "baseline-route-ipv6" for c in checks))   # baseline suffixed too
        self.assertTrue(self._no_ipv6_leak(checks))
        self.assertFalse(any(c["id"] == "ai-domain-route" and "2606:4700" in c["message"] for c in checks))

    def test_client_route_checker_ipv6_linux_routed(self):
        # The Linux `ip -6 route get` arm runs (dev tailscale0 -> tailscale -> OK), and the
        # baseline IPv6 finding is also suffixed.
        with tempfile.TemporaryDirectory() as tmp:
            result, payload = self._run_client_route_json(tmp, os_name="Linux", ipv6="routed")
        checks = payload["checks"]
        self.assertEqual(result.returncode, 0)
        ai6 = [c for c in checks if c["id"] == "ai-domain-route-ipv6"]
        # status OK (routed via tailscale0) -- locks the `-6` flag the same way the Darwin test
        # locks `-inet6`: dropping it makes the family-strict fake reject the IPv6 target, so
        # route6 returns empty and this becomes WARN instead of OK.
        self.assertTrue(any(c["status"] == "ok" and "2606:4700" in c["message"] for c in ai6))
        self.assertTrue(any(c["id"] == "baseline-route-ipv6" for c in checks))
        self.assertTrue(self._no_ipv6_leak(checks))

    def test_client_route_checker_ipv6_absent_aaaa_skips_clean(self):
        # No AAAA anywhere (AI + baseline both IPv4-only) -> the IPv6 pass records NOTHING and
        # never fails; exit stays 0. This is the exit-code-compatibility guard.
        with tempfile.TemporaryDirectory() as tmp:
            result, payload = self._run_client_route_json(tmp, ipv6=None)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(any(c["id"].endswith("-ipv6") for c in payload["checks"]))

    def test_client_route_checker_ipv6_local_route_warns_never_fails(self):
        # An AAAA that routes LOCALLY is advisory WARN, never FAIL; the script still exits 0.
        with tempfile.TemporaryDirectory() as tmp:
            result, payload = self._run_client_route_json(tmp, os_name="Darwin", ipv6="local")
        self.assertEqual(result.returncode, 0)
        ipv6 = [c for c in payload["checks"] if c["id"].endswith("-ipv6")]
        self.assertTrue(ipv6)                                       # the IPv6 pass ran
        self.assertTrue(any(c["status"] == "warn" for c in ipv6))   # advisory WARN
        self.assertFalse(any(c["status"] == "fail" for c in ipv6))  # and produced NO fail

    def test_client_route_checker_ipv6_unrouted_warns_never_fails(self):
        # AAAA present but the IPv6 route lookup returns NOTHING (unknown interface) -> advisory
        # WARN under ai-domain-route-ipv6, never FAIL; exit 0.
        with tempfile.TemporaryDirectory() as tmp:
            result, payload = self._run_client_route_json(tmp, os_name="Darwin", ipv6="noroute")
        self.assertEqual(result.returncode, 0)
        ai6 = [c for c in payload["checks"] if c["id"] == "ai-domain-route-ipv6"]
        self.assertTrue(ai6)
        self.assertTrue(any(c["status"] == "warn" for c in ai6))    # advisory WARN
        self.assertTrue(all(c["status"] != "fail" for c in ai6))    # never FAIL

    def test_client_route_checker_ipv6_partial_summary(self):
        # Mixed AAAA coverage emits the distinct ai-route-summary-ipv6 partial-coverage finding,
        # and each per-address IPv6 event stays under a *-ipv6 id (no leak to the IPv4 id).
        with tempfile.TemporaryDirectory() as tmp:
            result, payload = self._run_client_route_json(tmp, os_name="Darwin", ipv6="partial")
        checks = payload["checks"]
        self.assertEqual(result.returncode, 0)
        self.assertTrue(any(c["id"] == "ai-route-summary-ipv6" for c in checks))
        self.assertTrue(self._no_ipv6_leak(checks))

    def test_client_route_checker_ipv6_resolves_aaaa_once_per_domain(self):
        # Resolve-once: the IPv6 pass must issue exactly one AAAA query per domain (1 AI + 1
        # baseline = 2), guarding against a double-resolution regression.
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = self._run_client_route_json(tmp, os_name="Darwin", ipv6="routed")
            queries = self._dig_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(sum(1 for q in queries if q.endswith(" AAAA")), 2)

    def test_client_route_checker_ipv4_local_still_fails(self):
        # The CONVERSE of the IPv6 WARN default: an all-local IPv4 AI route must still FAIL and
        # exit 1 (the emit_domain_results `ai_unrouted_status` default is FAIL for IPv4). A run
        # can carry an IPv6 WARN at the same time without softening the IPv4 FAIL.
        with tempfile.TemporaryDirectory() as tmp:
            result, payload = self._run_client_route_json(tmp, os_name="Darwin", ipv6="local", ipv4_ai_local=True)
        self.assertEqual(result.returncode, 1)
        checks = payload["checks"]
        self.assertTrue(any(c["id"] == "ai-domain-route" and c["status"] == "fail" for c in checks))
        self.assertTrue(any(c["id"] == "ai-domain-route-ipv6" and c["status"] == "warn" for c in checks))

    def test_resolve_ipv6_all_getent_dedups(self):
        # Source-level unit test for the `getent ahostsv6` fallback (used when dig is absent):
        # override `have` to expose only getent, install a strict fake getent that repeats an
        # address per socket type, and assert resolve_ipv6_all returns the deduped address once.
        snippet = (
            "AI_EGRESS_SOURCE_ONLY=1 . ./check-client-routes.sh\n"
            'have() { [ "$1" = getent ]; }\n'
            'getent() { [ "$1" = ahostsv6 ] && [ "$2" = chatgpt.com ] || { echo "bad getent args: $*" >&2; return 2; }; '
            'printf "%s\\n" "2606:4700::a SOCK_STREAM chatgpt.com" "2606:4700::a SOCK_DGRAM chatgpt.com"; }\n'
            "resolve_ipv6_all chatgpt.com\n"
        )
        result = subprocess.run(["bash", "-c", snippet], cwd=str(ROOT), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout.split(), ["2606:4700::a"])   # deduped to exactly one

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

    def _diagnose_env(self, tmp, *, status_json=None, status_text=None, status_available=True, linux_userspace=False, os_name="Linux"):
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
        self._write_fake_command(fake_bin, "uname", f"#!/bin/sh\necho {os_name}\n")
        if os_name == "Darwin":
            # BSD route: exercises diagnose.sh's Darwin `route -n get` arm (argument-strict).
            self._write_fake_command(
                fake_bin,
                "route",
                """#!/bin/sh
[ "$1" = "-n" ] && [ "$2" = "get" ] || { echo "route: expected '-n get <ip>', got: $*" >&2; exit 64; }
eval "target=\\${$#}"
case "$target" in
  104.18.1.1) iface=utun8 ;;
  *) iface=en0 ;;
esac
echo "interface: $iface"
""",
            )

        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
        return env

    def test_diagnose_route_checker_darwin_success(self):
        # Closes the coverage gap: diagnose.sh's Darwin `route -n get` arm (diagnose.sh:298)
        # mirrors check-client-routes.sh but was only tested with uname=Linux. Drive it with
        # a fake uname=Darwin + an argument-strict BSD `route`, and assert the sample route
        # check reports the Darwin interface (proving the Darwin arm ran + parsed correctly).
        with tempfile.TemporaryDirectory() as tmp:
            domains = Path(tmp) / "domains.txt"
            domains.write_text("chatgpt.com\n", encoding="utf-8")
            env = self._diagnose_env(tmp, os_name="Darwin")
            result = subprocess.run(
                ["bash", str(ROOT / "diagnose.sh"), "--domains-file", str(domains), "--json"],
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
        payload = json.loads(result.stdout)
        route_checks = [c for c in payload["checks"] if c["id"] == "sample-domain-routes"]
        self.assertTrue(
            any(c["status"] == "ok" and "chatgpt.com -> 104.18.1.1 -> utun8" in c["message"] for c in route_checks),
            msg=f"Darwin route arm not exercised; checks={route_checks}",
        )

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

    def test_diagnose_custom_connector_tag_flag_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            domains = Path(tmp) / "domains.txt"
            domains.write_text("chatgpt.com\n", encoding="utf-8")
            env = self._diagnose_env(
                tmp,
                status_json={"Self": {"Online": True, "Tags": ["tag:custom-egress"], "AllowedIPs": []}},
                status_text="100.64.0.1 custom-host tag:custom-egress",
            )
            result = subprocess.run(
                ["bash", str(ROOT / "diagnose.sh"), "--domains-file", str(domains),
                 "--connector-tag", "tag:custom-egress", "--json"],
                env=env, text=True, capture_output=True, check=True,
            )
        checks = {c["id"]: c for c in json.loads(result.stdout)["checks"]}
        self.assertEqual(checks["connector-advertised"]["details"]["advertised"], True)

    def test_diagnose_custom_connector_tag_from_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            domains = Path(tmp) / "domains.txt"
            domains.write_text("chatgpt.com\n", encoding="utf-8")
            env = self._diagnose_env(
                tmp,
                status_json={"Self": {"Online": True, "Tags": ["tag:custom-egress"], "AllowedIPs": []}},
                status_text="100.64.0.1 custom-host tag:custom-egress",
            )
            env["CONNECTOR_TAG"] = "tag:custom-egress"
            result = subprocess.run(
                ["bash", str(ROOT / "diagnose.sh"), "--domains-file", str(domains), "--json"],
                env=env, text=True, capture_output=True, check=True,
            )
        checks = {c["id"]: c for c in json.loads(result.stdout)["checks"]}
        self.assertEqual(checks["connector-advertised"]["details"]["advertised"], True)

    def test_diagnose_connector_tag_from_identity_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            domains = Path(tmp) / "domains.txt"
            domains.write_text("chatgpt.com\n", encoding="utf-8")
            gen = Path(tmp) / "generated"
            gen.mkdir()
            (gen / "connector-identity.env").write_text("CONNECTOR_TAG=tag:custom-egress\n", encoding="utf-8")
            env = self._diagnose_env(
                tmp,
                status_json={"Self": {"Online": True, "Tags": ["tag:custom-egress"], "AllowedIPs": []}},
                status_text="100.64.0.1 custom-host tag:custom-egress",
            )
            env["GENERATED_DIR"] = str(gen)
            result = subprocess.run(
                ["bash", str(ROOT / "diagnose.sh"), "--domains-file", str(domains), "--json"],
                env=env, text=True, capture_output=True, check=True,
            )
        checks = {c["id"]: c for c in json.loads(result.stdout)["checks"]}
        self.assertEqual(checks["connector-advertised"]["details"]["advertised"], True)

    def test_diagnose_connector_tag_from_identity_file_without_trailing_newline(self):
        # Guards the `|| [ -n "$key" ]` read loop: a hand-written identity file
        # whose final CONNECTOR_TAG line lacks a trailing newline must still be
        # read (a plain `while read` would drop it and fall back to the convention).
        with tempfile.TemporaryDirectory() as tmp:
            domains = Path(tmp) / "domains.txt"
            domains.write_text("chatgpt.com\n", encoding="utf-8")
            gen = Path(tmp) / "generated"
            gen.mkdir()
            (gen / "connector-identity.env").write_text("CONNECTOR_TAG=tag:custom-egress", encoding="utf-8")
            env = self._diagnose_env(
                tmp,
                status_json={"Self": {"Online": True, "Tags": ["tag:custom-egress"], "AllowedIPs": []}},
                status_text="100.64.0.1 custom-host tag:custom-egress",
            )
            env["GENERATED_DIR"] = str(gen)
            result = subprocess.run(
                ["bash", str(ROOT / "diagnose.sh"), "--domains-file", str(domains), "--json"],
                env=env, text=True, capture_output=True, check=True,
            )
        checks = {c["id"]: c for c in json.loads(result.stdout)["checks"]}
        self.assertEqual(checks["connector-advertised"]["details"]["advertised"], True)

    def test_diagnose_custom_tag_expected_ignores_ai_egress_convention(self):
        # When a specific tag is expected, matching is exact: a stray tag:ai-egress-*
        # no longer auto-counts as the connector.
        with tempfile.TemporaryDirectory() as tmp:
            domains = Path(tmp) / "domains.txt"
            domains.write_text("chatgpt.com\n", encoding="utf-8")
            env = self._diagnose_env(
                tmp,
                status_json={"Self": {"Online": True, "Tags": ["tag:ai-egress-jp"], "AllowedIPs": []}},
                status_text="100.64.0.1 ai-egress-jp-01 tag:ai-egress-jp",
            )
            result = subprocess.run(
                ["bash", str(ROOT / "diagnose.sh"), "--domains-file", str(domains),
                 "--connector-tag", "tag:custom-egress", "--json"],
                env=env, text=True, capture_output=True, check=True,
            )
        checks = {c["id"]: c for c in json.loads(result.stdout)["checks"]}
        self.assertEqual(checks["connector-advertised"]["status"], "warn")

    def test_diagnose_rejects_invalid_connector_tag(self):
        result = subprocess.run(
            ["bash", str(ROOT / "diagnose.sh"), "--connector-tag", "notatag"],
            text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 2)
        # Assert the validator-specific message so this fails if the flag/validator
        # is removed (an unknown option also exits 2).
        self.assertIn("must be tag:", result.stderr)
        self.assertIn("got: notatag", result.stderr)

    def test_diagnose_custom_tag_text_fallback_is_exact(self):
        # The status-text fallback must be an exact field match: an expected tag of
        # tag:custom-egress must NOT match a different tag:custom-egress-extra.
        with tempfile.TemporaryDirectory() as tmp:
            domains = Path(tmp) / "domains.txt"
            domains.write_text("chatgpt.com\n", encoding="utf-8")
            env = self._diagnose_env(
                tmp,
                status_json={"Self": {"Online": True, "Tags": ["tag:custom-egress-extra"], "AllowedIPs": []}},
                status_text="100.64.0.1 custom-host tag:custom-egress-extra",
            )
            result = subprocess.run(
                ["bash", str(ROOT / "diagnose.sh"), "--domains-file", str(domains),
                 "--connector-tag", "tag:custom-egress", "--json"],
                env=env, text=True, capture_output=True, check=True,
            )
        checks = {c["id"]: c for c in json.loads(result.stdout)["checks"]}
        self.assertEqual(checks["connector-advertised"]["status"], "warn")

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
        self.assertIn("[OK] Connector advertised: expected connector tag is present.", result.stdout)

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
