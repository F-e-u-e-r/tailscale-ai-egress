import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import changelog_release_notes as notes
from scripts import policy_tool as tool

VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()

ENTRYPOINTS = [
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
]


class VersionConsistencyTests(unittest.TestCase):
    def test_version_file_is_semver(self):
        self.assertRegex(VERSION, r"^\d+\.\d+\.\d+$")

    def test_policy_tool_version_matches_version_file(self):
        self.assertEqual(tool.__version__, VERSION)

    def test_policy_tool_version_flag(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/policy_tool.py"), "--version"],
            text=True,
            capture_output=True,
            cwd=ROOT,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), f"tailscale-ai-egress policy_tool.py {VERSION}")

    def test_shell_entrypoints_report_version_cleanly(self):
        for script in ENTRYPOINTS:
            with self.subTest(script=script):
                result = subprocess.run(
                    ["bash", str(ROOT / script), "--version"],
                    text=True,
                    capture_output=True,
                    cwd=ROOT,
                )
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout.strip(), f"tailscale-ai-egress {script} {VERSION}")
                # Guards the Bash 3.2 cleanup-trap regression on early exit.
                self.assertNotIn("unbound variable", result.stderr)

    def test_shell_entrypoints_help_exits_zero(self):
        for script in ENTRYPOINTS:
            with self.subTest(script=script):
                result = subprocess.run(
                    ["bash", str(ROOT / script), "--help"],
                    text=True,
                    capture_output=True,
                    cwd=ROOT,
                )
                self.assertEqual(result.returncode, 0)
                self.assertNotIn("unbound variable", result.stderr)

    def test_shell_fallback_version_matches_version_file(self):
        # The standalone fallback constant must equal the VERSION file so a
        # script downloaded without the VERSION file still reports correctly.
        expected = 'VERSION="${VERSION:-' + VERSION + '}"'
        for script in ENTRYPOINTS:
            with self.subTest(script=script):
                text = (ROOT / script).read_text(encoding="utf-8")
                self.assertIn(expected, text)

    def test_shell_entrypoints_are_executable(self):
        for script in ENTRYPOINTS:
            with self.subTest(script=script):
                self.assertTrue(
                    os.access(ROOT / script, os.X_OK),
                    f"{script} lacks the executable bit (chmod +x / git update-index --chmod=+x)",
                )


class StabilityFreezeTests(unittest.TestCase):
    def test_manifest_schema_major_version_frozen(self):
        self.assertEqual(tool.MANIFEST_SCHEMA_VERSION, 1)

    def test_domain_pack_files_present(self):
        for file_name in (
            "default-ai-domains.json",
        ):
            with self.subTest(file_name=file_name):
                self.assertTrue((ROOT / "policy" / file_name).is_file())


class ChangelogTests(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    def test_changelog_has_section_for_current_version(self):
        section = notes.extract_section(self.text, VERSION)
        self.assertIsNotNone(section, f"CHANGELOG.md is missing a section for {VERSION}")
        self.assertTrue(section.strip())

    def test_changelog_section_stops_before_next_heading(self):
        section = notes.extract_section(self.text, VERSION)
        self.assertNotIn("## Prior to 1.0", section)

    def test_missing_version_returns_none(self):
        self.assertIsNone(notes.extract_section(self.text, "0.0.0-does-not-exist"))


class DocsLinkTests(unittest.TestCase):
    def test_all_local_doc_links_resolve(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/check_docs_links.py")],
            text=True,
            capture_output=True,
            cwd=ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
