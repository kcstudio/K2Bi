#!/usr/bin/env python3
"""Prepare an offline stop-fill proposal from explicit copied evidence.

There is deliberately no apply mode. Output is a review packet, never a command
for appending to a live journal. Broker authentication and execution are absent.
"""
from __future__ import annotations
import argparse
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from execution.connectors.types import BrokerExecution
from execution.engine.recovery import _positions_from_journal
from execution.engine.stop_reconciliation import StopIdentity, plan_stop_fill


def prepare(evidence: dict) -> dict:
    """Validate supplied evidence and show the exact accounting delta."""
    identity = StopIdentity(**evidence['identity'])
    raw = dict(evidence['execution'])
    raw['price'] = Decimal(raw['price'])
    raw['filled_at'] = datetime.fromisoformat(raw['filled_at'])
    execution = BrokerExecution(**raw)
    records = evidence['journal_records']
    proposal = plan_stop_fill(records, identity, execution,
        broker_qty=evidence['broker_quantity'],
        as_of=datetime.fromisoformat(evidence['as_of']))
    def positions(rows):
        return [dict(ticker=p.ticker,qty=p.qty,avg_price=str(p.avg_price))
                for p in _positions_from_journal(rows)]
    return {
        'mode':'prepare_only', 'runtime_writes':False,
        'identity':asdict(identity), 'proposed_record':proposal,
        'before':positions(records),
        'after':positions(records+([proposal] if proposal else [])),
        'already_applied':proposal is None,
        'fees':'unverified; no commission or net P&L asserted',
        'required_before_runtime_application':[
            'Fresh authoritative broker positions and stop identity confirmation',
            'Complete current journal replay and execution-ID conflict check',
            'Explicit approval for runtime application and separate strategy lifecycle handling',
        ],
    }


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('evidence',type=Path)
    args=parser.parse_args()
    print(json.dumps(prepare(json.loads(args.evidence.read_text())),indent=2))


if __name__ == '__main__':
    main()
