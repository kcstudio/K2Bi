"""Finite account-matched native cash capture through the leased Gateway helper."""
from __future__ import annotations
import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

ACCOUNT = 'DUQ220152'


def _now():
    return datetime.now(timezone.utc).isoformat()


def _collect(values):
    """Reject ambiguous native balances instead of collapsing duplicate rows."""
    rows, seen, readiness = [], set(), []
    for item in values:
        if item.account != ACCOUNT: continue
        tag = item.tag.removeprefix('$LEDGER-')
        if tag == 'AccountReady':
            text = str(item.value).lower()
            if text not in ('true','false'): raise ValueError('invalid account readiness')
            readiness.append(text=='true'); continue
        if tag != 'CashBalance': continue
        cur = item.currency
        if not isinstance(cur,str) or (cur!='BASE' and not re.fullmatch('[A-Z]{3}',cur)):
            raise ValueError('invalid currency')
        if cur in seen: raise ValueError('duplicate cash currency')
        seen.add(cur)
        if isinstance(item.value,bool): raise ValueError('boolean cash')
        try: amount = Decimal(str(item.value))
        except (InvalidOperation,TypeError,ValueError) as exc: raise ValueError('invalid cash') from exc
        text = format(amount,'f')
        if not amount.is_finite() or abs(amount)>Decimal('1e12') or not re.fullmatch(r'-?[0-9]{1,15}(?:\.[0-9]{1,8})?',text):
            raise ValueError('invalid cash precision or bounds')
        rows.append({'currency':cur,'value':text})
    if len(readiness)>1: raise ValueError('duplicate account readiness')
    if len(rows)>50: raise ValueError('too many cash currencies')
    return rows if rows else None, readiness[0] if readiness else None


def main(ib=None, fetch_fields=None, env=None) -> int:
    """Capture once, unsubscribe account updates and disconnect on every path."""
    started = _now(); subscribed = False
    result = {'schema':1,'kind':'ibkr-paper-currency-cash','origin':'recorded',
              'source':'IBKR paper Gateway','connection_mode':'readonly','gateway_port':4002,
              'requested_account_id':ACCOUNT,'account_id':None,'started_at':started,
              'completed_at':started,'status':'unavailable','reason':'capture unavailable',
              'account_ready':None,'cash':None}
    try:
        env = os.environ if env is None else env
        lease = int(env.get('K2BI_GATEWAY_CLIENT_ID',''))
        if not 90<=lease<=99: raise ValueError('invalid lease')
        if ib is None:
            from ib_async import IB
            from ib_async.ib import StartupFetch
            ib = IB(); fetch_fields = StartupFetch(0)
        ib.connect('127.0.0.1',4002,clientId=lease,readonly=True,timeout=10,
                   fetchFields=0 if fetch_fields is None else fetch_fields)
        if ACCOUNT not in ib.managedAccounts(): raise ValueError('paper account mismatch')
        ib.RequestTimeout = 15
        subscribed = True
        ib.reqAccountUpdates(ACCOUNT)
        cash, ready = _collect(ib.accountValues(ACCOUNT))
        result.update(status='complete',reason='' if cash is not None else 'native cash unknown',
                      account_id=ACCOUNT,cash=cash,account_ready=ready)
    except Exception as exc:
        result['reason'] = type(exc).__name__ + ': capture unavailable'
    finally:
        if ib is not None:
            if subscribed:
                try: ib.client.reqAccountUpdates(False,ACCOUNT)
                except Exception: pass
            try: ib.disconnect()
            except Exception: pass
        result['completed_at'] = _now()
    print(json.dumps(result)); return 0


if __name__ == '__main__':
    raise SystemExit(main())
