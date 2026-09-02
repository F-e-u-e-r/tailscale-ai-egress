import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import check_readme_parity as parity


class ReadmeParityTests(unittest.TestCase):
    def _root(self, tmp, *, en: str, zh: str, scripts=("fixture.sh",)):
        """A temp repo root: the named top-level scripts + the two READMEs."""
        root = Path(tmp)
        for name in scripts:
            (root / name).write_text("#!/bin/sh\n", encoding="utf-8")
        (root / "README.md").write_text(en, encoding="utf-8")
        (root / "README.zh-HK.md").write_text(zh, encoding="utf-8")
        return root

    def test_real_repo_readmes_are_in_parity(self):
        # The shipped READMEs must already agree (this is what CI enforces).
        self.assertEqual(parity.check_parity(ROOT), [])
        self.assertEqual(parity.main(["check_readme_parity.py", str(ROOT)]), 0)

    def test_readmes_actually_mention_failover_connectors(self):
        # Completeness, not just parity: the parity checker passes when a script
        # is omitted from BOTH READMEs, so pin the operator-facing mention of
        # the Model B switch entrypoint in each language explicitly.
        for name in ("README.md", "README.zh-HK.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("failover-connectors.sh", text, name)

    def test_symmetric_mention_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, en="Run `fixture.sh`.", zh="執行 `fixture.sh`。")
            self.assertEqual(parity.check_parity(root), [])

    def test_equal_omission_passes(self):
        # fixture.sh mentioned in NEITHER README -> parity, not a completeness error.
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, en="No entrypoints here.", zh="這裡沒有 entrypoint。")
            self.assertEqual(parity.check_parity(root), [])

    def test_en_only_mention_is_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, en="Run `fixture.sh`.", zh="沒有提到。")
            problems = parity.check_parity(root)
            self.assertTrue(any("fixture.sh" in p and "README.md" in p for p in problems), problems)
            self.assertEqual(parity.main(["check_readme_parity.py", str(root)]), 1)

    def test_zh_only_mention_is_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, en="Nothing here.", zh="執行 `fixture.sh`。")
            problems = parity.check_parity(root)
            self.assertTrue(any("fixture.sh" in p and "README.zh-HK.md" in p for p in problems), problems)

    def test_python_cli_drift_is_detected(self):
        # The hardcoded CLIs participate even without a file in the temp root.
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, en="See `policy_tool.py`.", zh="沒有提到。", scripts=())
            problems = parity.check_parity(root)
            self.assertTrue(any("policy_tool.py" in p for p in problems), problems)

    def test_boundary_false_matches_do_not_count_as_mention(self):
        # A different filename that merely contains the basename must NOT satisfy it.
        for other in (
            "refixture.sh", "old.fixture.sh", "fixture.sh-old", "fixture.sh.backup",
            "fixture.sh~", "fixture.sh.~1~",
        ):
            with self.subTest(other=other), tempfile.TemporaryDirectory() as tmp:
                # EN mentions the look-alike (not a real fixture.sh mention); zh mentions nothing.
                root = self._root(tmp, en=f"Run `{other}`.", zh="nothing")
                # Neither README truly mentions fixture.sh -> parity (both absent), no drift.
                self.assertEqual(parity.check_parity(root), [], f"{other} wrongly matched fixture.sh")

    def test_boundary_true_forms_count_as_mention(self):
        for form in ("`fixture.sh`", "./fixture.sh", "scripts/fixture.sh", "run fixture.sh.", "fixture.sh, then"):
            with self.subTest(form=form), tempfile.TemporaryDirectory() as tmp:
                # EN mentions fixture.sh in this form; zh does not -> drift proves the form matched.
                root = self._root(tmp, en=f"Here: {form}", zh="nothing")
                problems = parity.check_parity(root)
                self.assertTrue(
                    any("fixture.sh" in p for p in problems), f"{form} did not match fixture.sh"
                )


if __name__ == "__main__":
    unittest.main()
