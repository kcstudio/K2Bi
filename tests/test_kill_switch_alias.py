"""Tests for Q41 kill-switch kill.flag alias.

Covers the belt-and-suspenders alias path alongside the canonical `.killed`:
- alias-only trigger
- canonical-only trigger (regression)
- both-present trigger
- neither-present no-trigger (regression)
- symlink containment parity (NEW INVARIANT)
- production default-path behavior (no explicit path argument)
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from execution.risk import kill_switch as ks


class KillSwitchAliasTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.canonical = Path(self._tmp.name) / ".killed"
        self.alias = Path(self._tmp.name) / "kill.flag"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_canonical(self) -> None:
        ks.write_killed(reason="test", source="test", path=self.canonical)

    def _write_alias(self) -> None:
        ks.write_killed(reason="test", source="test", path=self.alias)

    def test_alias_only_trigger(self):
        """kill.flag exists, .killed absent -> killed=True."""
        self.assertFalse(ks.is_killed(self.canonical))
        self.assertFalse(ks.is_killed(self.alias))
        self._write_alias()
        self.assertTrue(ks.is_killed(self.alias))

    def test_canonical_only_trigger(self):
        """.killed exists, kill.flag absent -> killed=True (regression check)."""
        self._write_canonical()
        self.assertTrue(ks.is_killed(self.canonical))
        self.assertFalse(ks.is_killed(self.alias))

    def test_both_present_trigger(self):
        """Both files exist -> killed=True (canonical wins by check order)."""
        self._write_canonical()
        self._write_alias()
        self.assertTrue(ks.is_killed(self.canonical))
        self.assertTrue(ks.is_killed(self.alias))

    def test_neither_present_no_trigger(self):
        """Both absent -> killed=False (regression check)."""
        self.assertFalse(ks.is_killed(self.canonical))
        self.assertFalse(ks.is_killed(self.alias))

    def test_symlink_containment_rejected(self):
        """Symlinked path is rejected for both canonical and alias (NEW INVARIANT)."""
        # Create a real file outside the tmp dir
        outside = Path(self._tmp.name) / "outside"
        outside.write_text("{}", encoding="utf-8")

        # Canonical path as symlink -> rejected
        self.canonical.symlink_to(outside)
        self.assertFalse(ks.is_killed(self.canonical))

        # Alias path as symlink -> rejected
        self.alias.symlink_to(outside)
        self.assertFalse(ks.is_killed(self.alias))

    def test_assert_not_killed_alias_trigger(self):
        """assert_not_killed raises when alias is present."""
        self._write_alias()
        with self.assertRaises(ks.KillSwitchActiveError) as cm:
            ks.assert_not_killed(self.alias)
        self.assertEqual(cm.exception.record["reason"], "test")

    def test_assert_not_killed_canonical_trigger(self):
        """assert_not_killed raises when canonical is present (regression)."""
        self._write_canonical()
        with self.assertRaises(ks.KillSwitchActiveError) as cm:
            ks.assert_not_killed(self.canonical)
        self.assertEqual(cm.exception.record["reason"], "test")

    def test_production_default_path_canonical_trigger(self):
        """is_killed() with no args checks DEFAULT_KILL_PATH then alias (regression)."""
        self._write_canonical()
        with mock.patch.object(ks, "DEFAULT_KILL_PATH", self.canonical):
            with mock.patch.object(ks, "DEFAULT_KILL_PATH_ALIAS", self.alias):
                self.assertTrue(ks.is_killed())

    def test_production_default_path_alias_trigger(self):
        """is_killed() with no args detects alias when canonical absent."""
        self._write_alias()
        with mock.patch.object(ks, "DEFAULT_KILL_PATH", self.canonical):
            with mock.patch.object(ks, "DEFAULT_KILL_PATH_ALIAS", self.alias):
                self.assertTrue(ks.is_killed())

    def test_production_default_path_neither(self):
        """is_killed() with no args returns False when both absent."""
        with mock.patch.object(ks, "DEFAULT_KILL_PATH", self.canonical):
            with mock.patch.object(ks, "DEFAULT_KILL_PATH_ALIAS", self.alias):
                self.assertFalse(ks.is_killed())

    def test_read_kill_record_malformed_returns_none(self):
        """Malformed JSON returns None rather than crashing (fail-safe)."""
        self.canonical.write_text("not json", encoding="utf-8")
        self.assertIsNone(ks.read_kill_record(self.canonical))

    def test_assert_not_killed_malformed_record_still_raises(self):
        """assert_not_killed raises even when record is malformed."""
        self.canonical.write_text("not json", encoding="utf-8")
        with mock.patch.object(ks, "DEFAULT_KILL_PATH", self.canonical):
            with mock.patch.object(ks, "DEFAULT_KILL_PATH_ALIAS", self.alias):
                with self.assertRaises(ks.KillSwitchActiveError) as cm:
                    ks.assert_not_killed()
                # Record is None because read_kill_record swallowed the parse error
                self.assertIsNone(cm.exception.record)


if __name__ == "__main__":
    unittest.main()
