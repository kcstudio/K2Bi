"""Strict, side-effect-free planning for one proven protective stop execution.

This module never contacts a broker, appends a journal, or changes a strategy.
The caller must supply an authoritative position snapshot and owned stop identity.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from execution.connectors.types import BrokerExecution
from execution.engine.recovery import _positions_from_journal


@dataclass(frozen=True)
class StopIdentity:
    """Engine-owned ordinary STP child and the full position it protects."""

    ticker: str
    strategy: str
    parent_trade_id: str
    stop_perm_id: str
    stop_order_id: str
    quantity: int


def plan_stop_fill(
    records: list[dict[str, Any]], identity: StopIdentity,
    execution: BrokerExecution, *, broker_qty: int, as_of: datetime,
) -> dict[str, Any] | None:
    """Return a canonical full-close sell, or None for an exact durable replay.

    Invalid, conflicting or partial evidence raises ValueError. Commissions are
    intentionally absent: execution price does not prove the broker's fees.
    """
    for value in (identity.ticker, identity.strategy, identity.parent_trade_id):
        if not isinstance(value, str) or not value.strip():
            raise ValueError('missing stop ownership identity')
    for value in (identity.stop_perm_id, identity.stop_order_id):
        if not isinstance(value, str) or not value.isdigit() or int(value) <= 0:
            raise ValueError('missing positive stop broker identity')
    if type(identity.quantity) is not int or identity.quantity <= 0:
        raise ValueError('invalid protected quantity')
    if type(broker_qty) is not int or broker_qty != 0:
        raise ValueError('a verified zero broker position is required')
    if (not execution.exec_id or not isinstance(execution.exec_id, str)
            or execution.broker_perm_id != identity.stop_perm_id
            or execution.broker_order_id != identity.stop_order_id
            or execution.ticker != identity.ticker
            or execution.side.lower() not in ('sell', 'sld')
            or type(execution.qty) is not int
            or execution.qty != identity.quantity):
        raise ValueError('execution does not exactly close the identified stop')
    try:
        price = Decimal(str(execution.price))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError('invalid execution price') from exc
    if not price.is_finite() or price <= 0:
        raise ValueError('invalid execution price')
    if (execution.filled_at.tzinfo is None or as_of.tzinfo is None
            or execution.filled_at.utcoffset() is None or as_of.utcoffset() is None
            or execution.filled_at > as_of):
        raise ValueError('invalid execution timestamp')
    planned = {
        'event_type': 'order_filled', 'strategy': identity.strategy,
        'trade_id': f'{identity.parent_trade_id}:stop',
        'ticker': identity.ticker, 'side': 'sell', 'qty': identity.quantity,
        'broker_order_id': identity.stop_order_id,
        'broker_perm_id': identity.stop_perm_id,
        'payload': {
            'exec_id': execution.exec_id, 'fill_qty': identity.quantity,
            'fill_price': str(price), 'filled_at': execution.filled_at.isoformat(),
            'stop_reconciliation': {
                'parent_trade_id': identity.parent_trade_id,
                'source': 'verified_stop_execution',
            },
        },
    }
    duplicates = [r for r in records if r.get('event_type') == 'order_filled'
                  and (r.get('payload') or {}).get('exec_id') == execution.exec_id]
    projected = {p.ticker: p.qty for p in _positions_from_journal(records)}
    if duplicates:
        if len(duplicates) != 1 or any(duplicates[0].get(k) != v for k, v in planned.items()):
            raise ValueError('conflicting or repeated canonical execution ID')
        if projected.get(identity.ticker, 0) != 0:
            raise ValueError('canonical execution conflicts with position projection')
        return None
    if projected.get(identity.ticker, 0) != identity.quantity:
        raise ValueError('journal position does not match protected quantity')
    return planned


def read_stop_history(journal: Any) -> list[dict[str, Any]]:
    """Read accounting and stop barriers across rotations, failing on corruption.

    Used at startup and on a position disappearance, never on every normal tick.
    Each daily file is read under the writer's shared lock. Preserve file order.
    """
    from datetime import timezone
    from pathlib import Path

    records = []
    for path in sorted(Path(journal.base_dir).glob('*.jsonl')):
        try:
            when = datetime.fromisoformat(path.stem).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        for rec in journal.read_all_strict(when):
            if rec.get('event_type') in {
                'engine_recovered', 'order_filled', 'recovery_reconciled',
                'engine_stopped', 'strategy_stopped_out',
            }:
                records.append(rec)
    return records


def unresolved_stop_barriers(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep a halt until a later identity-matched canonical close exists.

    An unidentified barrier never auto-clears; it needs reviewed manual recovery.
    """
    pending = []
    for rec in records:
        payload = rec.get('payload') or {}
        if (rec.get('event_type') == 'engine_stopped'
                and payload.get('reason') == 'protective_stop_fill_unverified'):
            pending.append(rec)
        elif rec.get('event_type') == 'order_filled' and payload.get('stop_reconciliation'):
            pending = [r for r in pending if not (
                r.get('ticker') == rec.get('ticker')
                and r['payload'].get('quantity') == rec.get('qty')
                and bool(r.get('broker_perm_id'))
                and r.get('broker_perm_id') == rec.get('broker_perm_id')
                and bool(r['payload'].get('parent_trade_id'))
                and r['payload']['parent_trade_id'] == payload['stop_reconciliation'].get('parent_trade_id')
            )]
    return pending
