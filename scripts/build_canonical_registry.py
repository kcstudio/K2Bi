#!/usr/bin/env python3
"""CLI to build the canonical ticker registry from NASDAQ + NYSE CSVs.

Usage:
    python3 scripts/build_canonical_registry.py
    python3 -m scripts.build_canonical_registry
"""

from __future__ import annotations

import os
import sys

# Allow direct execution: python3 scripts/build_canonical_registry.py
_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from scripts.lib.canonical_ticker_registry import refresh_registry


def main() -> int:
    try:
        registry = refresh_registry()
        print(f"Canonical registry refreshed: {len(registry)} entries")
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
