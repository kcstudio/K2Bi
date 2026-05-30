"""P4-3 SSH stale-session cleanup runbook acceptance tests."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


VAULT_ROOT = Path(
    os.environ.get("K2BI_VAULT_ROOT", str(Path.home() / "Projects" / "K2Bi-Vault"))
)
RUNBOOK = VAULT_ROOT / "wiki" / "playbooks" / "playbook_ssh-stale-session-cleanup.md"


def _runbook_text() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def _shell_code_blocks(markdown: str) -> list[str]:
    return re.findall(r"```(?:bash|sh)\n(.*?)```", markdown, flags=re.DOTALL)


def test_p4_3_bug_ssh_notty_stuck_repro_is_documented() -> None:
    """P4-3-BUG-SSH-NOTTY-STUCK must be reproducible from the runbook."""
    text = _runbook_text()

    assert "P4-3-BUG-SSH-NOTTY-STUCK" in text
    assert "k2bi@notty" in text
    assert "two stale" in text.lower()
    assert "current SSH circuit status" in text
    assert "suspected stale `k2bi@notty` session count" in text
    assert "PID" in text
    assert "PPID" in text
    assert "TTY" in text
    assert "elapsed" in text.lower()
    assert "command" in text.lower()
    assert "confidence reason" in text.lower()
    assert "cleanup recommendation" in text.lower()
    assert "No automatic kill performed" in text
    assert "scripts/ssh-vps.sh exits `78`" in text
    assert "exit `78`" in text
    assert "If no output returns within 90 seconds" in text
    assert "etimes > 300" in text
    assert "older than the active operator `pts/` session" in text
    assert "K2BI_SSH_OVERRIDE" in text
    assert "Hostinger hPanel" in text


def test_p4_3_no_scheduler_or_auto_kill_surface() -> None:
    """P4-3 must not add an automatic killer, scheduler, or broker surface."""
    text = _runbook_text()
    shell_blocks = _shell_code_blocks(text)
    code = "\n".join(shell_blocks)

    assert len(shell_blocks) == 2
    for block in shell_blocks:
        assert block.strip().startswith("scripts/ssh-vps.sh ")
        parsed = subprocess.run(
            ["bash", "-n"],
            input=block,
            capture_output=True,
            text=True,
            check=False,
        )
        assert parsed.returncode == 0, parsed.stderr

    assert "no cron" in text.lower()
    assert "no launchagent" in text.lower()
    assert "no daemon" in text.lower()
    assert "no telegram" in text.lower()
    assert "no broker query" in text.lower()
    assert "no engine restart" in text.lower()
    assert "no `.killed` mutation" in text.lower()
    assert "no automatic kill performed" in text.lower()
    assert "Do not set `K2BI_SSH_OVERRIDE`" in text

    forbidden_command_patterns = [
        r"(^|[\s;&|/])kill\s+",
        r"\bcommand\s+kill\b",
        r"\beval\b",
        r"(^|[\s;&|])pkill\s+",
        r"(^|[\s;&|])killall\s+",
        r"\bsystemctl\s+restart\b",
        r"\bcrontab\b",
        r"\blaunchctl\b",
        r"\bscripts/gateway-query\.sh\b",
        r"\brm\s+.*\.killed\b",
        r"\btouch\s+.*\.killed\b",
    ]
    for pattern in forbidden_command_patterns:
        assert not re.search(pattern, code), pattern
