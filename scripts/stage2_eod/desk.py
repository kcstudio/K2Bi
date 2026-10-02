"""Render the accepted offline desk from a validated practice receipt."""
from __future__ import annotations

from html import escape
from pathlib import Path
import re
from . import ledger, store, inputs


def esc(value):
    """Escape all external text and attributes."""
    return escape(str(value), quote=True)


def table(headers, rows):
    """Render narrow-safe tabular records, with explicit empty state."""
    if not rows:
        return '<p class="tiny">None recorded.</p>'
    cells = ''.join('<tr>'+''.join(f'<td>{esc(cell)}</td>' for cell in row)+'</tr>' for row in rows)
    return '<div style="overflow-x:auto"><table style="width:100%;text-align:left"><thead><tr>'+''.join(f'<th>{esc(h)}</th>' for h in headers)+'</tr></thead><tbody>'+cells+'</tbody></table></div>'


def render(receipt: dict) -> str:
    """Use only sealed operational data, with explicit dates and origins."""
    store.validate_receipt(receipt)
    req, state = receipt['request'], receipt['state']
    evidence = req['evidence']
    fixture = evidence.get('origin') in ('synthetic_fixture', 'normalized_fixture') or any(b['origin'] == 'fixture' for b in req['bars'].values())
    label = 'INVENTED OFFLINE FIXTURE' if fixture else 'HISTORICAL SAVED EVIDENCE REPLAY'
    at = ledger.aware(req['as_of'])
    last = ledger.latest_session(at)
    research = [d for d in receipt['decisions'] if d['book'] == 'research']
    active = [d for d in research if d['action'] != 'wait']
    companies = []
    for symbol in ledger.SYMBOLS:
        analysis, bar = req['analyses'].get(symbol), req['bars'].get(symbol)
        decision = next(d for d in research if d['symbol'] == symbol)
        metadata = evidence.get('companies', {}).get(symbol, {})
        source = metadata.get('source', {})
        url = source.get('url')
        source_text = f"{source.get('kind', 'explicit normalized fixture' if fixture else 'unavailable')} · retrieved {source.get('retrieved_at', 'unavailable')}"
        source_html = esc(source_text)
        if url is not None and url == inputs.sec_url(symbol):
            source_html = f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{source_html}</a>'
        dated = f"Historical close ${bar['close']} · session {bar['session_date']} · received {bar['retrieved_at']} · {bar['source']} · {ledger.usable(bar, last) or 'eligible fixture, invented'}" if bar else 'Daily price unavailable'
        companies.append(f'<article class="card"><div class="ticker-name">{esc(symbol)} <small>{esc(metadata.get("industry", "Fixture" if fixture else "Unavailable"))}</small></div><p class="ticker-sub">{esc(analysis["status"]+" / "+analysis["stance"] if analysis else "paused / abstain")}</p><p>{esc(analysis["reason"] if analysis else "Missing research")}</p><p class="tiny">Research available {esc(analysis["available_at"] if analysis else "unavailable")}</p><p class="tiny">{source_html}</p><p class="hint">{esc(dated)}</p><p class="ticker-body"><strong>{esc(decision["action"])}:</strong> {esc(decision["reason"])}</p></article>')
    books = []
    for name in ('research', 'intraday'):
        book = state['books'][name]
        metrics = table(('Metric', 'USD'), [(k.replace('_', ' '), book[k]) for k in ledger.MONEY_KEYS])
        positions = table(('Symbol', 'Shares', 'Cost basis USD', 'Entry close', 'Mark session', 'Mark status'), [
            (s, p['quantity'], p['cost_basis'], p['entry_close'], book['last_prices'][s]['session_date'],
             ledger.usable(book['last_prices'][s], last) or 'eligible invented fixture') for s, p in sorted(book['positions'].items())])
        pending = table(('Symbol', 'Virtual action', 'Frozen shares', 'Queued UTC', 'Eligible close session'), [
            (s, p['side'], p['quantity'], p['queued_at'], p['eligible_session']) for s, p in sorted(book['pending'].items())])
        events = table(('Symbol', 'Virtual side', 'Shares', 'Fill USD', 'Fee USD', 'Cash total USD', 'Session', 'Recorded UTC'), [
            (e['symbol'], e['side'], e['quantity'], e['fill_price'], e['fee'], e['total'], e['session_date'], e['timestamp']) for e in book['events']])
        books.append(f'<article class="book"><h3>{esc(name.title())} practice book</h3>{metrics}<div class="zero"><strong>Virtual holdings</strong>{positions}</div><div class="zero"><strong>Pending later virtual actions</strong>{pending}</div><div class="zero"><strong>Transaction history</strong>{events}</div></article>')
    decision_table = table(('Book', 'Symbol', 'Virtual action', 'Reason', 'UTC evaluation'), [(d['book'], d['symbol'], d['action'], d['reason'], d['timestamp']) for d in receipt['decisions']])
    values = dict(
        STAMP=esc(f"Evaluation {req['as_of']}"),
        PROGRESS=f'<aside class="aside"><div class="eyebrow">{label}</div><strong>{esc(len(active))} research actions</strong><p>As of {esc(req["as_of"])}. Latest completed XNYS session {esc(last)}.</p><p class="small">These dates are historical input dates, never a current quote claim.</p></aside>',
        NOTICE=f'<div class="notice"><div class="mark">i</div><div><h2>{label}</h2><p>Virtual practice only. {"All prices and research in this fixture are invented." if fixture else "Saved IBKR daily prices remain display-only and cannot fill practice instructions."}</p></div></div>',
        SUMMARY=f'<div class="card"><h3>{"Virtual actions recorded" if active else "Waiting for usable evidence"}</h3><p>{esc("; ".join(d["symbol"]+": "+d["reason"] for d in research))}</p><button class="text-link" id="open-trade-guide" type="button">Open trade guidance</button></div>',
        COMPANY_STATUS=esc('Dated fixture' if fixture else 'Saved research'), COMPANIES='<div class="grid">'+''.join(companies)+'</div>',
        TRADE_DECISION='<div class="card"><h3>Recorded virtual decisions</h3>'+decision_table+'<p class="hint">A queued action waits for the first later NYSE session whose open is after its availability. Only that session close may fill; missed sessions cannot catch up. Quantity is frozen before the open.</p></div>',
        BOOKS='<div class="bookheads">'+''.join(books)+'</div>',
        PRACTICE_NOTE='<p class="callout">Whole shares, cash only, two positions maximum, 20% initial cash entry cost and 1% planned stop risk. Daily close stop 3%, target 5%; fills assume five basis points adverse slippage and $0.50 per transaction. Intraday remains empty with daily inputs.</p>',
        PAPER='<div class="paperbox"><div class="eyebrow">Broker snapshot unavailable</div><div class="title">Paper-account holdings unknown</div><p>No refreshed broker snapshot was loaded. Virtual cash, shares, fees and P&amp;L above are separate practice records.</p></div>',
        FOOTER=esc(f"Complete receipt {receipt['receipt_hash']} · input {receipt['request_sha256']} · evaluation {req['as_of']}"))
    template = Path(__file__).with_name('template.html').read_text(encoding='utf-8')
    return re.sub(r'\{\{([A-Z_]+)\}\}', lambda match: values[match.group(1)], template)
