"""Fixture-selection regressions, not validation of historical reader behavior."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from directory_history_fixture import HISTORY_ROOT_ENV, historical_package


class DirectoryHistoryFixtureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop(HISTORY_ROOT_ENV, None)

    def fixture(self, root):
        """Supply a package-shaped sentinel; it must never be imported here."""
        package = root / "candidate-v12"
        (package / "easysewer").mkdir(parents=True)
        (package / "easysewer" / "__init__.py").write_text(
            "raise AssertionError('selection must not import this sentinel')\n",
            encoding="utf-8",
        )
        return package

    def test_absent_default_root_skips_with_candidate_and_setup_reason(self):
        with self.assertRaises(unittest.SkipTest) as caught:
            historical_package("candidate-v12", default_root=self.root / "missing")
        self.assertIn("candidate-v12", str(caught.exception))
        self.assertIn(HISTORY_ROOT_ENV, str(caught.exception))
        self.assertIn(str(self.root / "missing"), str(caught.exception))

    def test_absent_default_candidate_skips_with_exact_path(self):
        with self.assertRaises(unittest.SkipTest) as caught:
            historical_package("candidate-v12", default_root=self.root)
        self.assertIn(str(self.root / "candidate-v12"), str(caught.exception))

    def test_explicit_absent_root_fails_without_default_fallback(self):
        self.fixture(self.root)
        os.environ[HISTORY_ROOT_ENV] = str(self.root / "missing")
        with self.assertRaisesRegex(AssertionError, "explicitly set.*does not exist"):
            historical_package("candidate-v12", default_root=self.root)

    def test_explicit_absent_candidate_fails(self):
        os.environ[HISTORY_ROOT_ENV] = str(self.root)
        with self.assertRaisesRegex(AssertionError, "candidate-v12.*does not exist"):
            historical_package("candidate-v12")

    def test_explicit_empty_root_fails(self):
        for setting in ("", " ", "\t"):
            with self.subTest(setting=setting):
                os.environ[HISTORY_ROOT_ENV] = setting
                with self.assertRaisesRegex(AssertionError, "explicitly set but empty"):
                    historical_package("candidate-v12", default_root=self.root)

    def test_explicit_file_instead_of_root_fails(self):
        invalid = self.root / "file"
        invalid.write_text("not a directory", encoding="utf-8")
        os.environ[HISTORY_ROOT_ENV] = str(invalid)
        with self.assertRaisesRegex(AssertionError, "root:.*not a directory"):
            historical_package("candidate-v12")

    def test_default_file_instead_of_root_fails(self):
        invalid = self.root / "file"
        invalid.write_text("not a directory", encoding="utf-8")
        with self.assertRaisesRegex(AssertionError, "root:.*not a directory"):
            historical_package("candidate-v12", default_root=invalid)

    def test_file_instead_of_candidate_fails_even_without_explicit_root(self):
        (self.root / "candidate-v12").write_text("not a package", encoding="utf-8")
        with self.assertRaisesRegex(AssertionError, "candidate-v12.*not a directory"):
            historical_package("candidate-v12", default_root=self.root)

    def test_incomplete_default_candidate_fails_instead_of_skipping(self):
        (self.root / "candidate-v12").mkdir()
        with self.assertRaisesRegex(AssertionError, "expected package file.*__init__.py"):
            historical_package("candidate-v12", default_root=self.root)

    def test_incomplete_explicit_candidate_fails(self):
        (self.root / "candidate-v12" / "easysewer").mkdir(parents=True)
        os.environ[HISTORY_ROOT_ENV] = str(self.root)
        with self.assertRaisesRegex(AssertionError, "expected package file.*__init__.py"):
            historical_package("candidate-v12")

    def test_initializer_directory_is_not_a_package_file(self):
        (self.root / "candidate-v12" / "easysewer" / "__init__.py").mkdir(parents=True)
        os.environ[HISTORY_ROOT_ENV] = str(self.root)
        with self.assertRaisesRegex(AssertionError, "expected package file.*__init__.py"):
            historical_package("candidate-v12")

    def test_default_fixture_returns_import_root_without_importing(self):
        expected = self.fixture(self.root)
        self.assertEqual(historical_package("candidate-v12", default_root=self.root), expected)

    def test_explicit_fixture_takes_precedence_over_default(self):
        self.fixture(self.root / "default")
        expected = self.fixture(self.root / "configured")
        os.environ[HISTORY_ROOT_ENV] = str(self.root / "configured")
        self.assertEqual(
            historical_package("candidate-v12", default_root=self.root / "default"),
            expected,
        )


if __name__ == "__main__":
    unittest.main()
