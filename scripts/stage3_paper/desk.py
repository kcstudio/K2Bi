"""Stage 3 desk renderer wrapping the accepted EOD renderer."""
from __future__ import annotations

import html
import re
import json
from zoneinfo import ZoneInfo
from scripts.stage3_paper import reference as _ref, action_card as _card

from scripts.stage2_eod import desk as _eod
from scripts.stage3_paper import snapshot as _snap
from scripts.stage3_paper import readiness as _ready
from scripts.stage3_paper import account_readiness as _account

_PANEL_RE = re.compile(
    r'<section\b(?=[^>]*\bid="panel-paper")[^>]*>.*?</section>', re.DOTALL)


def _esc(value):
    return html.escape(str(value), quote=True)


def _hkt(value):
    return _snap._ts(value, 'display time').astimezone(ZoneInfo('Asia/Hong_Kong')).strftime('%d %b %Y %H:%M HKT') if value else 'unknown'


def _action_html(card):
    fields = [('Paper account', card['account']), ('Candidate', card['candidate']),
              ('Quantity', card['quantity']), ('Estimated cost', card['estimated_cost']),
              ('Estimated risk', card['estimated_risk'])]
    rows = ''.join(_row(k, 'Withheld' if v is None else v) for k, v in fields)
    checks = ''.join('<tr><th>' + _esc(c['label'].replace('_', ' ').capitalize().replace('Usd', 'USD'))
                     + '</th><td>' + _esc(c['status'].replace('_', ' ')) + '</td><td>'
                     + _esc(c['detail']) + '</td></tr>' for c in card['checks'])
    return ('<h3>' + ('NO PROPOSAL: WAIT' if card['status'] == 'WAIT' else _esc(card['status']))
            + '</h3><p>' + _esc(card['reason']) + '</p><p>Next step: ' + _esc(card['next_step'])
            + '</p><div class="st3-scroll"><table>' + rows + '</table><table>' + checks
            + '</table></div><details><summary>Risk context details</summary><p>Validators not run.</p><pre>'
            + _esc(json.dumps(card['risk_config'], sort_keys=True)) + '</pre></details>')


def _row(key, val):
    return "<tr><th scope=\"row\">" + _esc(key) + "</th><td>" + _esc(val) + "</td></tr>"


def _value_row(label, value):
    if value is None:
        return ("<tr><th scope=\"row\">" + _esc(label) + "</th>"
                "<td class=\"st3-unknown\">unknown</td>"
                "<td class=\"st3-unknown\">unknown</td></tr>")
    return ("<tr><th scope=\"row\">" + _esc(label) + "</th><td>" + _esc(value["currency"])
            + "</td><td>" + _esc(value["value"]) + "</td></tr>")


def _prices_html(prices):
    if prices is None:
        return ''
    rows = []
    for r in prices.get("rows", []):
        if r.get("state") == "available":
            reason = "stale: session older than 1 completed session" if r.get("stale") else "dated history"
            rows.append("<tr><td>" + _esc(r.get("symbol")) + "</td><td>"
                        + _esc(r.get("session_date")) + "</td><td>" + _esc(r.get("close"))
                        + "</td><td>" + _esc(r.get("session_lag")) + "</td><td>"
                        + _esc(reason) + "</td></tr>")
        else:
            rows.append("<tr><td>" + _esc(r.get("symbol")) + "</td><td class=\"st3-unknown\">"
                        + _esc(r.get("state")) + "</td><td class=\"st3-unknown\">"
                        + _esc(r.get("reason", "")) + "</td><td></td><td></td></tr>")
    return ('<h4>Tracked-company closing prices</h4>'
            '<p>' + ('Recorded capture and request settings match this saved price file; historical reference only.' if prices.get('provenance') == 'recorded_request_verified' else 'INVENTED OFFLINE EXAMPLE.' if prices.get('provenance') == 'invented_fixture' else 'Price input provenance is unverified: this legacy file does not distinguish recorded from invented prices. Inspect the saved source receipt.') + '</p>'
            '<p class="st3-note">Historical closing prices only; not current quotes; '
            'not prices for actual paper holdings and implying no trade eligibility. Feed entitlement remains unverified. Adjustment: ' + _esc(prices.get('adjustment', 'unverified')) + '.</p>'
            '<div class="st3-scroll"><table class="st3-prices"><thead><tr>'
            '<th scope="col">Company</th><th scope="col">Last completed dated session</th>'
            '<th scope="col">USD close</th><th scope="col">Session lag</th>'
            '<th scope="col">Session status</th></tr></thead><tbody>'
            + "".join(rows) + '</tbody></table></div>'
            '<p class="st3-scope">Price capture ' + _esc(_hkt(prices.get("completed_at", "")))
            + '; evaluation ' + _esc(_hkt(prices.get("as_of", ""))) + '. Research panels remain the saved research replay; refresh is manual.</p>'
            + '<details><summary>Price source details</summary>' + '<table>' + _row('Captured at', prices['completed_at']) + _row('Evaluated at', prices['as_of']) + _row('Raw sha256', prices['raw_sha256']) + _row('Proof sha256', prices.get('proof_sha256', 'unknown')) + '</table></details>')


def _cash_html(cash):
    if cash is None:
        return ''
    banner = ''
    if cash.get("origin") == "fixture":
        banner = '<p class="st3-banner st3-fixture">INVENTED OFFLINE BROKER FIXTURE.</p>'
    rows = []
    for r in cash.get("rows", []):
        rows.append("<tr><td>" + _esc(r["currency"]) + "</td><td>" + _esc(r["value"])
                    + "</td></tr>")
    if cash.get("status") != "complete" or cash.get("cash") is None:
        body = ('<p class="st3-unknown">Cash unavailable: ' + _esc(cash.get("reason", ""))
                + '</p>')
    elif cash.get("known_empty"):
        body = '<p class="st3-knownzero">No currency balances were reported.</p>'
    else:
        body = ('<div class="st3-scroll"><table class="st3-cash"><thead><tr>'
                '<th scope="col">Native currency</th><th scope="col">Cash</th></tr></thead>'
                '<tbody>' + "".join(rows) + '</tbody></table></div>')
    caveat = 'These balances do not establish money available for an order.'
    if cash.get("account_ready") is not True:
        caveat += ' The broker reliability flag is false or unknown: cash reliability is unproven.'
    has_usd = any(r["currency"] == "USD" for r in cash.get("rows", []))
    if not has_usd:
        caveat += ' Unknown USD: no USD balance reported.'
    return (banner + '<h4>Recorded paper currency cash</h4><p>' + _esc(cash['freshness']) + '</p>' + body
            + '<p class="st3-scope">' + _esc(caveat) + ' BASE is never USD.</p>'
            + '<p class="st3-scope">Captured ' + _esc(_hkt(cash.get("completed_at", "")))
            + '; evaluated ' + _esc(_hkt(cash.get("as_of", ""))) + '; refresh is manual.</p>'
            + '<details><summary>Currency cash source details</summary><table>' + _row('Captured at', cash['completed_at']) + _row('Evaluated at', cash['as_of']) + _row('Raw sha256', cash['raw_sha256']) + _row('AccountReady', cash['account_ready']) + '</table></details>')


def _account_html(data):
    if data is None:
        return '<h4>Account funding and visible orders</h4><p>Not captured: settled USD, pending orders and restrictions are unknown.</p>'
    account, orders = data['account'], data['orders']
    state = data['orders_state']
    visibility = ('No visible API orders at capture' if state == 'no_visible_api_orders' else 'Visible API orders at capture' if state == 'visible_api_orders' else 'API order coverage unknown')
    values = ''.join(_row(r['tag'].removeprefix('$LEDGER-') + ' (' + (r['currency'] or 'unspecified currency') + ')', r['value']) for r in (account['values'] or []) if account) if account else ''
    rows = ''.join('<tr><td>' + _esc(r['symbol']) + '</td><td>' + _esc(r['side']) + '</td><td>' + _esc(r['quantity']) + '</td><td>' + _esc(r['currency']) + '</td><td>' + _esc(r['limit_price'] if r['limit_price'] is not None else 'unknown') + '</td><td>' + _esc(r['status']) + '</td></tr>' for r in (orders['rows'] or [])) if orders else ''
    origin = 'INVENTED OFFLINE FIXTURE' if data['origin'] == 'fixture' else 'Recorded request verified' if data['provenance'] == 'recorded_request_verified' else 'Source provenance unverified'
    table = '<div class="st3-scroll"><table><tr><th>Symbol</th><th>Side</th><th>Reported quantity</th><th>Currency</th><th>Reported limit</th><th>Status</th></tr>' + rows + '</table></div>' if rows else ''
    details = '<div class="st3-scroll"><table>' + values + '</table></div><pre>' + _esc(json.dumps({'request': data['request'], 'account': account, 'orders': orders, 'raw_sha256': data['raw_sha256'], 'proof_sha256': data['proof_sha256']}, sort_keys=True)) + '</pre>'
    return ('<h4>Account funding and visible orders</h4><p>Saved readiness snapshot: ' + _esc(_hkt(data['completed_at'])) + '; evaluated ' + _esc(_hkt(data['as_of'])) + '; ' + _esc(data['freshness']) + '; refresh is manual.</p><p>' + _esc(origin) + '. Settled USD funding: unknown. Reserved commitments: unknown. Trading permissions and restrictions: unknown.</p><p>' + _esc(visibility) + '. This covers API-visible orders only, not guaranteed absence of all manual/user orders or permission to trade. Reported limits are not current execution quotes.</p>' + table + '<p>Account and orders were captured separately, not atomically. Broker reliability flag at account capture: ' + _esc(account['account_ready'] if account else 'unknown') + '. AccountReady is not approval. Cash is not profit or loss; HKD and BASE are not spendable USD proof.</p><details><summary>Readiness source, balances and order details</summary><p>Settlement totals retain reported currency; native settled USD scope remains unverified.</p>' + details + '</details>')


def _body(parsed, prices=None, cash=None, card=None, account=None):
    banner = ""
    if parsed is None:
        return ('<div class="st3-wrap"><style>.st3-wrap{min-width:0;overflow-wrap:anywhere;}.st3-scroll{overflow-x:auto;max-width:100%;}.st3-wrap pre{white-space:pre-wrap;overflow-wrap:anywhere;}</style><p class="st3-unknown">No paper snapshot provided; '
                'paper positions and account values are unknown.</p>' + _action_html(card) + _account_html(account) + _prices_html(prices) + _cash_html(cash) + '</div>')
    if parsed["origin"] == "fixture":
        banner = ('<p class="st3-banner st3-fixture" role="note">INVENTED OFFLINE BROKER FIXTURE. '
                  'Not captured from a live Gateway.</p>')
    elif parsed["freshness"] == "stale":
        banner = ('<p class="st3-banner st3-stale" role="note">STALE snapshot, captured earlier than '
                  'the freshness window and never current.</p>')
    rows = [_row("Snapshot status", parsed["status"]), _row("Origin", parsed["origin"]),
            _row("Freshness", parsed["freshness"]), _row("Source", parsed["source"]),
            _row("Account", parsed["account_id"] or "unknown"),
            _row("Requested account", parsed["requested_account_id"]),
            _row("Connection mode", parsed["connection_mode"]),
            _row("Gateway port", parsed["gateway_port"]),
            _row("Started at", parsed["started_at"]), _row("Completed at", parsed["completed_at"]),
            _row("Evaluated at", parsed["evaluated_at"]), _row("Raw sha256", parsed["raw_sha256"])]
    positions = parsed["positions"]
    if positions is None:
        positions_html = ('<p class="st3-unknown">Paper positions unknown for this snapshot. '
                          'Refused to invent zero holdings.</p>')
    elif not positions:
        positions_html = ('<p class="st3-knownzero">Paper positions captured as a known empty list; '
                          'no paper holdings reported at capture.</p>')
    else:
        body = "".join("<tr><td>" + _esc(p["symbol"]) + "</td><td>" + _esc(p["sec_type"])
                       + "</td><td>" + _esc(p["exchange"]) + "</td><td>" + _esc(p["quantity"])
                       + "</td><td>" + _esc(p["currency"]) + "</td><td>" + _esc(p["average_cost"])
                       + "</td></tr>" for p in positions)
        positions_html = ('<p class="st3-note">Actual paper holdings without current price or value; '
                          'separate from virtual books and implying no adoption or orders.</p>'
                          '<div class="st3-scroll"><table class="st3-positions">'
                          '<thead><tr><th scope="col">Symbol</th><th scope="col">Type</th>'
                          '<th scope="col">Exchange</th><th scope="col">Quantity</th>'
                          '<th scope="col">Currency</th><th scope="col">Average acquisition cost</th>'
                          '</tr></thead><tbody>' + body + '</tbody></table></div>')
    values = parsed["account_values"]
    if values is None:
        values_html = ('<p class="st3-unknown">Account values unknown for this snapshot; '
                       'not converted and not summed.</p>')
    else:
        values_html = ('<div class="st3-scroll"><table class="st3-values">'
                       '<thead><tr><th scope="col">Balance</th><th scope="col">Currency</th>'
                       '<th scope="col">Value</th></tr></thead><tbody>'
                       + _value_row("Cash", values["TotalCashValue"])
                       + _value_row("Estimated account value", values["NetLiquidation"])
                       + _value_row("Funds available (broker-reported)", values["AvailableFunds"])
                       + '</tbody></table></div><p class="st3-note">Currencies shown separately and '
                       'never summed or converted; null is unknown, not zero.</p>')
    reason = ""
    if parsed["reason"]:
        reason = '<p class="st3-partial">Partial capture note: ' + _esc(parsed["reason"]) + '</p>'
    outer = ('<div class="st3-wrap"><style>.st3-wrap{min-width:0;overflow-wrap:anywhere;word-break:break-word;}'
             '.st3-scroll{overflow-x:auto;max-width:100%;}.st3-banner{padding:.5rem;border:1px solid currentColor;'
             'font-weight:bold;}.st3-fixture{background:#ffe8b3;}.st3-stale{background:#e8e8e8;}'
             '.st3-wrap th,.st3-wrap td{padding:.5rem .8rem;text-align:left;}.st3-wrap pre{white-space:pre-wrap;overflow-wrap:anywhere;}.st3-unknown{font-style:italic;}@media(max-width:390px){.st3-wrap{max-width:100%;}}</style>')
    prices_html = _prices_html(prices)
    cash_html = _cash_html(cash)
    return (outer + banner + _action_html(card) + _account_html(account) + '<h3>Paper account snapshot</h3><p>Saved account snapshot: '
            + _esc(_hkt(parsed['completed_at'])) + '; evaluated ' + _esc(_hkt(parsed['evaluated_at']))
            + '; refresh is manual.</p>' + reason
            + '<h4>Paper holdings</h4>' + positions_html + '<h4>Account balances</h4>' + values_html
            + prices_html + cash_html
            + '<details class="st3-details"><summary>Source and capture details</summary>'
            + '<div class="st3-scroll"><table class="st3-meta"><tbody>' + "".join(rows)
            + _row("Cash source tag", "TotalCashValue")
            + _row("Estimated account value source tag", "NetLiquidation")
            + _row("Funds available source tag", "AvailableFunds")
            + '</tbody></table></div></details>'
            + '<p class="st3-scope">Read only paper snapshot view; not a live feed; no live quotes, trade '
              'eligibility, or orders are claimed.</p></div>')


def render(receipt, snapshot_raw, *, as_of, prices_raw=None, cash_raw=None, proof_raw=None, config_raw=None, example=None, account_raw=None, account_proof_raw=None):
    _snap._ts(as_of, "as_of")
    base = _eod.render(receipt)
    parsed = _snap.parse_snapshot(snapshot_raw, as_of=as_of) if snapshot_raw is not None else None
    if proof_raw is not None and prices_raw is None: raise ValueError('price proof needs prices')
    prices = _ref.verify_reference(prices_raw, proof_raw, as_of=as_of) if prices_raw is not None else None
    cash = _ready.cash_readiness(cash_raw, as_of=as_of) if cash_raw is not None else None
    if account_proof_raw is not None and account_raw is None: raise ValueError("account proof needs readiness source")
    account = _account.parse_account_readiness(account_raw, as_of=as_of, proof_raw=account_proof_raw) if account_raw is not None else None
    match = _PANEL_RE.search(base)
    if match is None or _PANEL_RE.search(base, match.end()) is not None:
        raise ValueError("accepted template needs exactly one panel-paper section")
    original = match.group(0)
    open_tag = original[:original.index(">") + 1]
    card = _card.build_card(prices, cash, parsed, config_raw=config_raw, example=example, account_readiness=account)
    replacement = open_tag + _body(parsed, prices, cash, card, account) + "</section>"
    return base[:match.start()] + replacement + base[match.end():]
