"""Private recovery-context token for recovery-only broker actions."""

from __future__ import annotations


_RECOVERY_CONTEXT_TOKEN = object()


def is_recovery_context_token(candidate: object) -> bool:
    """Return True only for the module-private recovery token."""

    return candidate is _RECOVERY_CONTEXT_TOKEN


__all__ = ["is_recovery_context_token"]
