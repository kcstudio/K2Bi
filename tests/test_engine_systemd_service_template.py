"""Tests for the repo-tracked k2bi-engine systemd unit template.

The live VPS unit is operator-owned. These tests only validate the
repository template that the PR will hand to the operator for gated
deployment.
"""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE_TEMPLATE = ROOT / "deploy" / "k2bi-engine.service"
DEPLOY_NOTE = ROOT / "deploy" / "k2bi-engine-service-deploy.md"


def _service_assignments() -> dict[str, str]:
    assignments: dict[str, str] = {}
    for raw in SERVICE_TEMPLATE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        assignments[key.strip()] = value.strip()
    return assignments


class EngineSystemdServiceTemplateTests(unittest.TestCase):
    def test_template_restarts_after_clean_exit_but_keeps_burst_limit(self):
        self.assertTrue(
            SERVICE_TEMPLATE.exists(),
            f"missing repo-tracked service template: {SERVICE_TEMPLATE}",
        )
        assignments = _service_assignments()
        self.assertEqual(assignments.get("Restart"), "always")
        self.assertNotEqual(assignments.get("Restart"), "on-failure")
        self.assertEqual(assignments.get("RestartSec"), "30")
        self.assertEqual(assignments.get("StartLimitIntervalSec"), "10")
        self.assertEqual(assignments.get("StartLimitBurst"), "5")

    def test_operator_deploy_note_documents_gated_vps_install(self):
        self.assertTrue(
            DEPLOY_NOTE.exists(),
            f"missing operator deploy note: {DEPLOY_NOTE}",
        )
        text = DEPLOY_NOTE.read_text(encoding="utf-8")
        self.assertIn("/etc/systemd/system/k2bi-engine.service", text)
        self.assertIn("systemctl daemon-reload", text)
        self.assertIn("operator", text.lower())
        self.assertIn("Restart=always", text)
        self.assertIn(
            "systemctl show k2bi-engine.service -p Restart "
            "-p StartLimitIntervalSec -p StartLimitBurst -p RestartSec",
            text,
        )


if __name__ == "__main__":
    unittest.main()
