"""Tests for shared watchlist-index writer with file locking (m2.22 F4)."""

from __future__ import annotations

import multiprocessing as mp
import tempfile
import unittest
from pathlib import Path

from scripts.lib.watchlist_index import _index_lock, update_watchlist_index


class UpdateWatchlistIndexBasicTests(unittest.TestCase):
    def test_creates_new_index(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            update_watchlist_index(td_path, "NVDA", "2026-04-26", "promoted")
            index_path = td_path / "wiki" / "watchlist" / "index.md"
            self.assertTrue(index_path.exists())
            content = index_path.read_text()
            self.assertIn("| Symbol | Date | Status |", content)
            self.assertIn("| [[NVDA]] | 2026-04-26 | promoted |", content)

    def test_appends_to_existing_index(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            update_watchlist_index(td_path, "NVDA", "2026-04-26", "promoted")
            update_watchlist_index(td_path, "LRCX", "2026-04-26", "screened")
            content = (td_path / "wiki" / "watchlist" / "index.md").read_text()
            self.assertIn("| [[NVDA]]", content)
            self.assertIn("| [[LRCX]]", content)

    def test_idempotent_on_existing_symbol(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            update_watchlist_index(td_path, "NVDA", "2026-04-26", "promoted")
            content_before = (td_path / "wiki" / "watchlist" / "index.md").read_text()
            update_watchlist_index(td_path, "NVDA", "2026-04-27", "screened")
            content_after = (td_path / "wiki" / "watchlist" / "index.md").read_text()
            self.assertEqual(content_before, content_after)

    def test_lock_sentinel_created(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            update_watchlist_index(td_path, "NVDA", "2026-04-26", "promoted")
            lock_path = td_path / "wiki" / "watchlist" / ".index.lock"
            self.assertTrue(lock_path.exists())


class IndexLockContextManagerTests(unittest.TestCase):
    def test_lock_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            index_path = Path(td) / "deeply" / "nested" / "wiki" / "watchlist" / "index.md"
            with _index_lock(index_path):
                self.assertTrue(index_path.parent.exists())
            self.assertTrue((index_path.parent / ".index.lock").exists())

    def test_lock_released_on_exception(self):
        with tempfile.TemporaryDirectory() as td:
            index_path = Path(td) / "wiki" / "watchlist" / "index.md"
            try:
                with _index_lock(index_path):
                    raise RuntimeError("simulated failure")
            except RuntimeError:
                pass
            # Re-acquire must succeed (lock was released by context manager exit).
            with _index_lock(index_path):
                pass


# ---------------------------------------------------------------------------
# Concurrency proof (m2.22 F4): two processes racing on the same index must
# both have their rows survive after both complete.
# ---------------------------------------------------------------------------


def _writer_process(vault_path_str: str, symbol: str, date: str, status: str):
    """Worker target -- imports must happen inside (mp.spawn fork-context)."""
    from scripts.lib.watchlist_index import update_watchlist_index as fn

    fn(Path(vault_path_str), symbol, date, status)


class ConcurrentWritersDoNotDropRowsTests(unittest.TestCase):
    def test_two_processes_both_rows_survive(self):
        """Without locking, two concurrent writers can each read the same
        old index and the second atomic replace drops the first update.
        With locking, both rows must survive."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            # Pre-create an empty index dir so the first writer's mkdir is a no-op
            # in the racy critical section.
            (td_path / "wiki" / "watchlist").mkdir(parents=True, exist_ok=True)

            ctx = mp.get_context("spawn")
            p1 = ctx.Process(
                target=_writer_process,
                args=(str(td_path), "NVDA", "2026-04-26", "promoted"),
            )
            p2 = ctx.Process(
                target=_writer_process,
                args=(str(td_path), "LRCX", "2026-04-26", "screened"),
            )
            p1.start()
            p2.start()
            p1.join(timeout=30)
            p2.join(timeout=30)
            self.assertEqual(p1.exitcode, 0, "Writer 1 failed")
            self.assertEqual(p2.exitcode, 0, "Writer 2 failed")

            content = (td_path / "wiki" / "watchlist" / "index.md").read_text()
            self.assertIn(
                "| [[NVDA]]", content, "NVDA row was dropped by the race"
            )
            self.assertIn(
                "| [[LRCX]]", content, "LRCX row was dropped by the race"
            )


if __name__ == "__main__":
    unittest.main()
