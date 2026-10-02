"""Pure, causal daily-close practice ledger. No broker or network operations."""
from __future__ import annotations

import copy
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
import exchange_calendars as xcals

SYMBOLS = ('G', 'CALX', 'CDNS', 'SPY', 'XLV')
MONEY_KEYS = ('initial_cash', 'cash', 'market_value', 'equity', 'realized_pnl',
              'unrealized_pnl', 'net_pnl', 'fees')
BAR_KEYS = set('symbol source source_ref session_date retrieved_at currency close adjustment_policy market_data_type raw_sha256 resolution trade_eligible origin'.split())
ANALYSIS_KEYS = set('symbol status stance available_at reason receipt_hash'.split())
FEE = Decimal('.50')


def keys(obj, expected, label):
    """Require an exact mapping shape."""
    if not isinstance(obj, dict) or set(obj) != set(expected):
        raise ValueError(f'{label}: invalid keys or type')


def text(value):
    """Require a nonempty string."""
    if not isinstance(value, str) or not value:
        raise ValueError('nonempty string required')
    return value


def aware(value):
    """Parse a timestamp with an explicit offset, normalized to UTC."""
    text(value)
    try:
        result = datetime.fromisoformat(value)
        if result.utcoffset() is None:
            raise ValueError('timezone required')
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise ValueError('invalid offset timestamp') from exc


def iso(value):
    """Serialize UTC explicitly."""
    return value.isoformat().replace('+00:00', 'Z')


def number(value, *, signed=False):
    """Require bounded finite plain decimal strings."""
    pattern = r'-?[0-9]{1,15}(?:\.[0-9]{1,12})?' if signed else r'[0-9]{1,15}(?:\.[0-9]{1,12})?'
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError('plain decimal string required')
    return Decimal(value)


def price(value):
    """Validate a positive daily price."""
    result = number(value)
    if not 0 < result <= 1000000:
        raise ValueError('price outside bounds')
    return result


def money(value):
    """Round cash once to cents."""
    return format(value.quantize(Decimal('.01'), rounding=ROUND_HALF_UP), '.2f')


def sha(value):
    """Validate a lowercase SHA-256 digest."""
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('invalid SHA-256')
    return value


@lru_cache(maxsize=1)
def calendar():
    """Use the real XNYS calendar, with no fallback."""
    try:
        return xcals.get_calendar('XNYS')
    except Exception as exc:
        raise ValueError('XNYS calendar unavailable') from exc


def session(value):
    """Validate a canonical real session date."""
    text(value)
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError('noncanonical date')
        return calendar().date_to_session(value)
    except Exception as exc:
        raise ValueError(f'not an XNYS session: {value}') from exc


def latest_session(at):
    """Find the latest completed session in a bounded calendar window."""
    try:
        days = calendar().sessions_in_range((at.date() - timedelta(days=21)).isoformat(), at.date().isoformat())
        done = [d for d in days if calendar().session_close(d).to_pydatetime() <= at]
        if not done:
            raise ValueError('no completed session')
        return done[-1].date().isoformat()
    except Exception as exc:
        raise ValueError('completed XNYS session unavailable') from exc


def eligible(at):
    """First session whose open is strictly after decision availability."""
    try:
        days = calendar().sessions_in_range(at.date().isoformat(), (at.date() + timedelta(days=21)).isoformat())
        for day in days:
            if calendar().session_open(day).to_pydatetime() > at:
                return day.date().isoformat()
    except Exception as exc:
        raise ValueError('next XNYS open unavailable') from exc
    raise ValueError('next XNYS open unavailable')


def validate_inputs(as_of, analyses, bars):
    """Reject malformed normalized inputs before any state change."""
    at = aware(as_of)
    last = latest_session(at)
    for mapping in (analyses, bars):
        if not isinstance(mapping, dict) or not set(mapping) <= set(SYMBOLS):
            raise ValueError('unknown symbol or invalid input mapping')
    for symbol, row in analyses.items():
        keys(row, ANALYSIS_KEYS, 'analysis')
        if row['symbol'] != symbol or row['status'] not in ('fresh', 'paused') or row['stance'] not in ('support', 'abstain', 'invalidate'):
            raise ValueError('invalid analysis identity/status')
        if row['status'] == 'paused' and row['stance'] != 'abstain':
            raise ValueError('paused analysis cannot have a stance')
        if aware(row['available_at']) > at:
            raise ValueError('future analysis')
        text(row['reason']); sha(row['receipt_hash'])
    for symbol, row in bars.items():
        keys(row, BAR_KEYS, 'bar')
        if row['symbol'] != symbol or row['currency'] != 'USD' or row['resolution'] != 'daily':
            raise ValueError('invalid bar identity/currency/resolution')
        price(row['close']); sha(row['raw_sha256'])
        day = session(row['session_date'])
        received = aware(row['retrieved_at'])
        if not calendar().session_close(day).to_pydatetime() <= received <= at:
            raise ValueError('bar close/availability violates causality')
        if type(row['trade_eligible']) is not bool or row['adjustment_policy'] not in ('unadjusted', 'adjusted', 'unverified'):
            raise ValueError('invalid bar eligibility/adjustment')
        if row['origin'] == 'fixture':
            if row['source'] != 'fixture' or row['source_ref'] != f'fixture:{symbol}' or row['market_data_type'] != 'historical':
                raise ValueError('fixture source mismatch')
        elif row['origin'] == 'recorded':
            if row['source'] != 'ibkr_gateway' or not isinstance(row['source_ref'], str) or not re.fullmatch(r'ibkr:contract/[1-9][0-9]*', row['source_ref']):
                raise ValueError('recorded source mismatch')
            if row['trade_eligible'] or row['adjustment_policy'] != 'unverified' or row['market_data_type'] != 'unverified':
                raise ValueError('saved IBKR evidence is display-only')
        else:
            raise ValueError('unknown bar origin')
    return at, last


def usable(row, last):
    """Explain whether a mark is usable for a virtual action."""
    if row is None:
        return 'missing daily price'
    if row['session_date'] != last:
        return f"stale daily price from {row['session_date']}"
    if not row['trade_eligible'] or row['adjustment_policy'] != 'unadjusted':
        return 'display-only price; eligibility or adjustment unverified'
    return None


def initial_state():
    """Return independent $10,000 empty practice books."""
    book = {key: ('10000.00' if key in ('initial_cash', 'cash', 'equity') else '0.00') for key in MONEY_KEYS}
    book.update(positions={}, pending={}, last_prices={}, events=[])
    return {'last_cycle': None, 'books': {'research': copy.deepcopy(book), 'intraday': copy.deepcopy(book)}}


def fill_price(close, side):
    """Five basis points adverse slippage, retained at four decimals."""
    factor = Decimal('1.0005') if side == 'BUY' else Decimal('.9995')
    return format((price(close) * factor).quantize(Decimal('.0001'), rounding=ROUND_HALF_UP), '.4f')


def entry_allowed(book, quantity, close):
    cost = Decimal(money(Decimal(fill_price(close, 'BUY')) * quantity + FEE))
    risk = price(close) * quantity * Decimal('.03') + FEE * 2
    return cost <= 2000 and cost <= Decimal(book['cash']) and risk <= 100 and len(book['positions']) < 2


def rebuild_book(events, marks):
    """Reconstruct holdings, fee-inclusive basis and money from transactions."""
    cash, fees, realized = Decimal('10000'), Decimal(0), Decimal(0)
    positions = {}
    previous = None
    for event in events:
        keys(event, set('symbol side quantity close fill_price fee total timestamp session_date intent'.split()), 'event')
        symbol, side, q = event['symbol'], event['side'], event['quantity']
        if symbol not in SYMBOLS or side not in ('BUY', 'SELL') or type(q) is not int or not 0 < q <= 1000000:
            raise ValueError('invalid transaction')
        at = aware(event['timestamp'])
        if previous is not None and at < previous:
            raise ValueError('transaction chronology')
        previous = at
        day = session(event['session_date'])
        next_open = calendar().session_open(session(eligible(calendar().session_close(day).to_pydatetime()))).to_pydatetime()
        if calendar().session_close(day).to_pydatetime() > at or at >= next_open:
            raise ValueError('transaction before close')
        validate_intent(event['intent'], symbol, at)
        intent = event['intent']
        if intent['side'] != side or intent['quantity'] != q or intent['eligible_session'] != event['session_date']:
            raise ValueError('transaction does not match frozen intent')
        if event['fill_price'] != fill_price(event['close'], side) or event['fee'] != '.50':
            raise ValueError('transaction cost assumption changed')
        total = money(Decimal(event['fill_price']) * q + (FEE if side == 'BUY' else -FEE))
        if event['total'] != total:
            raise ValueError('transaction total mismatch')
        if side == 'BUY':
            if symbol in positions or not entry_allowed({'cash': money(cash), 'positions': positions}, q, event['close']):
                raise ValueError('entry limits violated')
            positions[symbol] = {'quantity': q, 'cost_basis': total, 'entry_close': event['close'], 'opened_at': event['timestamp']}
            cash -= Decimal(total)
        else:
            if symbol not in positions or positions[symbol]['quantity'] != q:
                raise ValueError('uncovered sale')
            realized += Decimal(total) - Decimal(positions.pop(symbol)['cost_basis'])
            cash += Decimal(total)
        fees += FEE
    market = Decimal(0)
    for symbol, pos in positions.items():
        if symbol not in marks:
            raise ValueError('held position missing dated mark')
        market += price(marks[symbol]['close']) * pos['quantity']
    market = Decimal(money(market))
    equity = cash + market
    basis = sum((Decimal(p['cost_basis']) for p in positions.values()), Decimal(0))
    return dict(initial_cash='10000.00', cash=money(cash), market_value=money(market), equity=money(equity),
                realized_pnl=money(realized), unrealized_pnl=money(market-basis), net_pnl=money(equity-10000), fees=money(fees), positions=positions)


def validate_intent(intent, symbol, at):
    keys(intent, set('symbol side quantity queued_at eligible_session reference_close source source_ref origin'.split()), 'intent')
    q = intent['quantity']
    if intent['symbol'] != symbol or intent['side'] not in ('BUY', 'SELL') or type(q) is not int or not 0 < q <= 1000000:
        raise ValueError('invalid intent identity/quantity')
    queued = aware(intent['queued_at'])
    if queued > at or intent['eligible_session'] != eligible(queued):
        raise ValueError('invalid intent eligibility/chronology')
    close = price(intent['reference_close'])
    if intent['side'] == 'BUY' and q != int((Decimal('2000')-FEE) / (close * Decimal('1.0005'))):
        raise ValueError('intent quantity not frozen by entry budget')
    if intent['origin'] != 'fixture' or intent['source'] != 'fixture' or intent['source_ref'] != f'fixture:{symbol}':
        raise ValueError('intent source not eligible')


def reconcile(state):
    """Fail closed on altered money, holdings, transactions or frozen intent."""
    keys(state, ('last_cycle', 'books'), 'state')
    keys(state['books'], ('research', 'intraday'), 'books')
    at = aware(state['last_cycle']) if state['last_cycle'] is not None else None
    for name, book in state['books'].items():
        keys(book, (*MONEY_KEYS, 'positions', 'pending', 'last_prices', 'events'), 'book')
        for field in ('positions', 'pending', 'last_prices'):
            if not isinstance(book[field], dict) or not set(book[field]) <= set(SYMBOLS):
                raise ValueError('invalid book mapping')
        if not isinstance(book['events'], list):
            raise ValueError('invalid events')
        if at is None and (book['pending'] or book['events'] or book['last_prices']):
            raise ValueError('initial state has history')
        if at:
            validate_inputs(iso(at), {}, book['last_prices'])
        expected = rebuild_book(book['events'], book['last_prices'])
        if any(book[key] != value for key, value in expected.items()):
            raise ValueError('book accounting does not reconcile')
        if at:
            for symbol, intent in book['pending'].items():
                validate_intent(intent, symbol, at)
                if intent['side'] == 'BUY' and symbol in book['positions']:
                    raise ValueError('pending buy already held')
                if intent['side'] == 'SELL' and (symbol not in book['positions'] or book['positions'][symbol]['quantity'] != intent['quantity']):
                    raise ValueError('pending sell uncovered')
            if any(aware(e['timestamp']) > at for e in book['events']):
                raise ValueError('future event')
        if name == 'intraday' and (book['events'] or book['pending'] or book['positions'] or book['last_prices']):
            raise ValueError('intraday daily-data activity forbidden')


def advance(state, *, as_of, analyses, bars):
    """Advance one cycle with exactly one decision per book and symbol."""
    reconcile(state)
    at, last = validate_inputs(as_of, analyses, bars)
    if state['last_cycle'] is not None and at <= aware(state['last_cycle']):
        raise ValueError('cycles must strictly increase')
    result = copy.deepcopy(state)
    result['last_cycle'] = iso(at)
    book = result['books']['research']
    decisions = []
    for symbol in SYMBOLS:
        row, analysis = bars.get(symbol), analyses.get(symbol)
        if row is not None:
            old = book['last_prices'].get(symbol)
            if old is None or row['session_date'] >= old['session_date']:
                book['last_prices'][symbol] = copy.deepcopy(row)
        reason = usable(row, last)
        action = 'wait'
        pending = book['pending'].get(symbol)
        processed = False
        if pending:
            target = pending['eligible_session']
            if last >= target:
                processed = True
                next_open = calendar().session_open(session(eligible(calendar().session_close(session(target)).to_pydatetime()))).to_pydatetime()
                if last != target or at >= next_open:
                    reason = 'missed eligible session; frozen instruction expired'
                    del book['pending'][symbol]
                elif reason:
                    reason = f'pending virtual {pending["side"].lower()}: {reason}'
                elif (row['source'], row['source_ref'], row['origin']) != (pending['source'], pending['source_ref'], pending['origin']):
                    reason = 'pending source identity mismatch'
                elif pending['side'] == 'BUY' and not entry_allowed(book, pending['quantity'], row['close']):
                    reason = 'frozen quantity rejected: later cost, cash or risk limit'
                    del book['pending'][symbol]
                else:
                    side, q = pending['side'], pending['quantity']
                    fp = fill_price(row['close'], side)
                    total = money(Decimal(fp)*q + (FEE if side == 'BUY' else -FEE))
                    book['events'].append(dict(symbol=symbol, side=side, quantity=q, close=row['close'], fill_price=fp,
                        fee='.50', total=total, timestamp=iso(at), session_date=row['session_date'], intent=copy.deepcopy(pending)))
                    del book['pending'][symbol]
                    book.update(rebuild_book(book['events'], book['last_prices']))
                    action, reason = side.lower(), f'virtual {side.lower()} filled at later session close; frozen quantity'
            else:
                processed, reason = True, f'pending virtual {pending["side"].lower()} waits for {target} close'
        if not processed:
            pos = book['positions'].get(symbol)
            if reason:
                pass
            elif pos:
                trigger = (price(row['close']) <= price(pos['entry_close'])*Decimal('.97') or price(row['close']) >= price(pos['entry_close'])*Decimal('1.05'))
                invalidate = analysis and analysis['status'] == 'fresh' and analysis['stance'] == 'invalidate'
                if trigger or invalidate:
                    action, reason = 'queue_sell', 'close stop/target or fresh invalidation; later-session virtual exit'
                else:
                    action, reason = 'hold', 'hold virtual position; close exit conditions not met'
            elif not analysis or analysis['status'] != 'fresh' or analysis['stance'] != 'support':
                reason = analysis['reason'] if analysis else 'missing research; paused'
            else:
                q = int((Decimal('2000')-FEE)/(price(row['close'])*Decimal('1.0005')))
                reserved = sum(1 for i in book['pending'].values() if i['side'] == 'BUY')
                if q > 0 and entry_allowed(book, q, row['close']) and len(book['positions'])+reserved < 2:
                    action, reason = 'queue_buy', 'support with eligible daily close; frozen later-session virtual entry'
                else:
                    reason = 'entry cash, whole-share, position or stop-risk limit'
            if action in ('queue_buy', 'queue_sell'):
                q = q if action == 'queue_buy' else pos['quantity']
                queued = max(at, aware(row['retrieved_at']), aware(analysis['available_at']) if analysis else at)
                book['pending'][symbol] = dict(symbol=symbol, side='BUY' if action == 'queue_buy' else 'SELL', quantity=q,
                    queued_at=iso(queued), eligible_session=eligible(queued), reference_close=row['close'],
                    source=row['source'], source_ref=row['source_ref'], origin=row['origin'])
        decisions.append(dict(book='research', symbol=symbol, action=action, reason=reason, timestamp=iso(at)))
    book.update(rebuild_book(book['events'], book['last_prices']))
    decisions.extend(dict(book='intraday', symbol=s, action='wait', reason='daily bars cannot support intraday practice', timestamp=iso(at)) for s in SYMBOLS)
    reconcile(result)
    return {'state': result, 'decisions': decisions}
