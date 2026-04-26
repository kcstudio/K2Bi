"""Shared watchlist-index writer with cross-process file locking.

Both invest-narrative Ship 2 (`promote_to_watchlist`) and m2.13
invest-screen (`enrich`, `manual_promote`) mutate
``wiki/watchlist/index.md``. Before this module both ships had
byte-identical private copies of the same read/modify/write helper
without coordination, so two concurrent runs could each read the same
old index, append a different row, and the second atomic replace would
drop the first update. m2.22 review re-evaluated the deferred TOCTOU
finding from m2.13 R-final and confirmed cross-ship exposure made the
race materially worse.

Locking is per-machine (POSIX ``fcntl.flock`` on a sentinel file
colocated with the index). Syncthing replicates files between machines
but does NOT replicate lock state, so cross-machine concurrent writes
remain best-effort. The single-machine case is the realistic concern
(two terminal sessions, a cron + manual run, etc.) and is the surface
this lock closes.
"""

from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path

import yaml

from scripts.lib.strategy_frontmatter import atomic_write_bytes


@contextmanager
def _index_lock(index_path: Path):
    """Acquire an exclusive flock on a sentinel file beside ``index_path``.

    The sentinel ``.index.lock`` is created lazily and persists across
    runs (it is a coordination handle, not data). Callers MUST do
    read+write inside the ``with`` block so the read/modify/write cycle
    is serialized.
    """
    lock_path = index_path.parent / ".index.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch(exist_ok=True)
    with open(lock_path, "r+") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)


def update_watchlist_index(vault: Path, symbol: str, date: str, status: str) -> None:
    """Insert or refresh a watchlist row in ``wiki/watchlist/index.md``.

    Idempotent on ``symbol`` (existing row matched on ``| [[SYMBOL]]``
    prefix is left untouched). Read+modify+write is serialized via
    ``_index_lock`` to keep two concurrent writers from dropping each
    other's update.
    """
    index_path = vault / "wiki" / "watchlist" / "index.md"
    entry_line = f"| [[{symbol}]] | {date} | {status} |"

    with _index_lock(index_path):
        if index_path.exists():
            content = index_path.read_text()
            if f"| [[{symbol}]]" in content:
                return
            lines = content.splitlines()
            insert_pos = len(lines)
            in_table = False
            for i, line in enumerate(lines):
                if line.startswith("| Symbol"):
                    in_table = True
                elif in_table and not line.startswith("|"):
                    insert_pos = i
                    break
            lines.insert(insert_pos, entry_line)
            new_content = "\n".join(lines) + "\n"
        else:
            frontmatter = {
                "tags": ["watchlist", "index", "k2bi"],
                "date": date,
                "type": "index",
                "origin": "k2bi-generate",
                "up": "[[index]]",
            }
            fm_lines = ["---"]
            fm_lines.extend(yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).splitlines())
            fm_lines.append("---")
            new_content = "\n".join(fm_lines) + "\n\n# Watchlist Index\n\n"
            new_content += "| Symbol | Date | Status |\n|---|---|---|\n"
            new_content += entry_line + "\n"

        atomic_write_bytes(index_path, new_content.encode("utf-8"))
