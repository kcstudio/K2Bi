"""Spec B §5 tests for the MasterClientID=99 operator convention."""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class MasterClientIdDocumentationTests(unittest.TestCase):
    def test_claude_names_client_99_as_master_client_id(self) -> None:
        text = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")

        self.assertIn("clientId `1` is reserved for the engine", text)
        self.assertIn("clientId `90-98`", text)
        self.assertIn(
            "clientId `99` is MasterClientID",
            text,
        )
        self.assertIn(
            "operator read+cancel privileges across all clients",
            text,
        )

    def test_gateway_query_documents_client_99_operator_default(self) -> None:
        text = (REPO_ROOT / "scripts" / "gateway-query.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("clientId=99", text)
        self.assertIn("operator MasterClientID", text)

    def test_vps_context_documents_master_client_config_and_manual_test(
        self,
    ) -> None:
        text = (
            REPO_ROOT
            / "wiki"
            / "context"
            / "context_ibkr-secondary-user-vps.md"
        ).read_text(encoding="utf-8")

        self.assertIn("MasterClientID=99", text)
        self.assertIn("/home/ibgateway/ibc/config.ini", text)
        self.assertIn("clientId=88", text)
        self.assertIn("clientId=99", text)
        self.assertIn("reqAllOpenOrders()", text)
        self.assertIn("cancelOrder", text)
        self.assertIn("operator manual test", text.lower())


if __name__ == "__main__":
    unittest.main()
