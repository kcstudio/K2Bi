"""Stage 2 daily quote validation package.

This package exposes the pure offline daily bar validator used by Stage 2.
"""

from .contract import validate_daily_bar

__all__ = ["validate_daily_bar"]
