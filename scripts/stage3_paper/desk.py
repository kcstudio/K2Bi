"""Stage 3 desk renderer wrapping the accepted EOD renderer."""
from __future__ import annotations

import html
import re

from scripts.stage2_eod import desk as _eod
from scripts.stage3_paper import snapshot as _snap
from scripts.stage3_paper import readiness as _ready

_PANEL_RE = re.compile(
    r'<section\b(?=[^>]*\bid="panel-paper")[^>]*>.*?</section>', re.DOTALL)


def _esc(value):
    return html.escape(str(value), quote=True)


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
            '<p>Price input provenance is unverified: this legacy file does not distinguish recorded from invented prices. Inspect the saved source receipt.</p>'
            '<p class="st3-note">Historical closing prices only; not current quotes; '
            'not prices for actual paper holdings and implying no trade eligibility. Adjustment treatment and feed entitlement remain unverified.</p>'
            '<div class="st3-scroll"><table class="st3-prices"><thead><tr>'
            '<th scope="col">Company</th><th scope="col">Last completed dated session</th>'
            '<th scope="col">USD close</th><th scope="col">Session lag</th>'
            '<th scope="col">Session status</th></tr></thead><tbody>'
            + "".join(rows) + '</tbody></table></div>'
            '<p class="st3-scope">Price capture ' + _esc(prices.get("completed_at", ""))
            + '; evaluation ' + _esc(prices.get("as_of", "")) + '. Research panels remain the saved research replay; refresh is manual.</p>'
            + '<details><summary>Price source details</summary>' + '<table>' + _row('Raw sha256', prices['raw_sha256']) + '</table></details>')


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
            + '<p class="st3-scope">Captured ' + _esc(cash.get("completed_at", ""))
            + '; evaluated ' + _esc(cash.get("as_of", "")) + '; refresh is manual.</p>'
            + '<details><summary>Currency cash source details</summary><table>' + _row('Raw sha256', cash['raw_sha256']) + _row('AccountReady', cash['account_ready']) + '</table></details>')


def _body(parsed, prices=None, cash=None):
    banner = ""
    if parsed is None:
        return ('<div class="st3-wrap"><style>.st3-wrap{min-width:0;overflow-wrap:anywhere;}.st3-scroll{overflow-x:auto;max-width:100%;}</style><p class="st3-unknown">No paper snapshot provided; '
                'paper positions and account values are unknown.</p>' + _prices_html(prices) + _cash_html(cash) + '</div>')
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
             '.st3-unknown{font-style:italic;}@media(max-width:390px){.st3-wrap{max-width:100%;}}</style>')
    prices_html = _prices_html(prices)
    cash_html = _cash_html(cash)
    return (outer + banner + '<h3>Paper account snapshot</h3><p>Saved account snapshot: '
            + _esc(parsed['completed_at']) + '; evaluated ' + _esc(parsed['evaluated_at'])
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


def render(receipt, snapshot_raw, *, as_of, prices_raw=None, cash_raw=None):
    _snap._ts(as_of, "as_of")
    base = _eod.render(receipt)
    parsed = _snap.parse_snapshot(snapshot_raw, as_of=as_of) if snapshot_raw is not None else None
    prices = _ready.price_readiness(prices_raw, as_of=as_of) if prices_raw is not None else None
    cash = _ready.cash_readiness(cash_raw, as_of=as_of) if cash_raw is not None else None
    match = _PANEL_RE.search(base)
    if match is None or _PANEL_RE.search(base, match.end()) is not None:
        raise ValueError("accepted template needs exactly one panel-paper section")
    original = match.group(0)
    open_tag = original[:original.index(">") + 1]
    replacement = open_tag + _body(parsed, prices, cash) + "</section>"
    return base[:match.start()] + replacement + base[match.end():]
