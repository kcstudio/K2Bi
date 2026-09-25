"""Render an HTML research-desk report from a report mapping.

The module is intentionally dependency-free: it converts an already-computed
report dict into a single self-contained HTML string. All untrusted text is
escaped for HTML attributes/markup, and data passed to the inline script is
encoded as JSON with dangerous HTML characters replaced by unicode escape
sequences so it remains inert inside script text.
"""
import json
from decimal import Decimal, InvalidOperation
from html import escape as _html_escape


def _esc(s: str) -> str:
    """Escape text for safe insertion into HTML markup or attributes."""
    return _html_escape(str(s), quote=True)


def _safe_json(obj) -> str:
    """JSON-encode data for a script context, escaping HTML-sensitive chars."""
    raw = json.dumps(obj, ensure_ascii=False)
    return (raw.replace("&", "\\u0026")
               .replace("<", "\\u003c")
               .replace(">", "\\u003e")
               .replace("\u2028", "\\u2028")
               .replace("\u2029", "\\u2029"))

def _to_decimal(v):
    """Parse a numeric value (possibly a formatted money string) or return None."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return Decimal(v)
    if isinstance(v, float):
        return Decimal(str(v))
    if isinstance(v, Decimal):
        return v
    if isinstance(v, str):
        cleaned = v.strip().replace("$", "").replace(",", "")
        if not cleaned:
            return None
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return None
    return None


def _fmt_money(v, currency="$", places=2):
    """Format a money value; strings are parsed as numbers so they are not shown raw."""
    num = _to_decimal(v)
    if num is None:
        return "unavailable"
    return f"{currency}{num:,.{places}f}"


def _fmt_pct(v):
    """Format a percentage value. Strings already containing % are preserved."""
    if v is None:
        return "unavailable"
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return "unavailable"
        if s.endswith("%"):
            return s
        num = _to_decimal(s)
        if num is None:
            return s
        return f"{num:.2f}%"
    num = _to_decimal(v)
    if num is None:
        return "unavailable"
    return f"{num * Decimal(100):.2f}%"

def _status_class(status: str) -> str:
    s = (status or "").lower()
    if "paused" in s or "stale" in s:
        return "st-paused"
    if "fresh" in s or "active" in s or "clear" in s:
        return "st-fresh"
    if "missing" in s or "blocked" in s or "risk" in s:
        return "st-missing"
    return "st-neutral"

def render(report: dict) -> str:
    as_of = _esc(str(report.get("as_of", "unavailable")))
    sha = _esc(str(report.get("input_sha256", "unavailable")))
    mode = _esc(str(report.get("mode", "")))
    assumptions = report.get("assumptions", [])
    warnings = report.get("warnings", [])
    industries = report.get("industries", [])
    watchlist = report.get("watchlist", [])
    books = report.get("books", {})
    decisions = report.get("decisions", [])

    # Build per-book, per-symbol decision index preserving input array order.
    decision_index = {}
    for d in decisions:
        key = (d.get("book"), d.get("symbol"))
        decision_index.setdefault(key, []).append(d)

    def latest_decision(book_key, symbol):
        """Return the last decision by array order (not max timestamp)."""
        ds = decision_index.get((book_key, symbol))
        return ds[-1] if ds else None

    book_order = list(books.keys()) if books else ["research", "intraday"]
    book_keys = [k for k in book_order if k in books]

    def book_label(k):
        b = books.get(k, {})
        return _esc(str(b.get("label", k)))

    ind_options = ['<option value="all">All industries (' + str(len(watchlist)) + ")</option>"]
    for ind in industries:
        i = _esc(str(ind.get("industry", "")))
        c = _esc(str(ind.get("count", 0)))
        ind_options.append(f'<option value="{i}">{i} ({c})</option>')

    initial_book = book_keys[0] if book_keys else "research"

    wl_book_options = []
    for k in book_keys:
        sel = " selected" if k == initial_book else ""
        wl_book_options.append(f'<option value="{_esc(k)}"{sel}>{book_label(k)}</option>')

    log_book_options = []
    for k in book_keys:
        sel = " selected" if k == "research" else ""
        log_book_options.append(f'<option value="{_esc(k)}"{sel}>{book_label(k)} | decision log filter</option>')

    # Watchlist rows
    wl_rows = []
    for w in watchlist:
        sym = _esc(str(w.get("symbol", "")))
        name = _esc(str(w.get("name", "")))
        ind = w.get("industry", "")
        ind_e = _esc(str(ind))
        status = "Paused"
        next_cond = "unavailable"
        latest = latest_decision(initial_book, w.get("symbol"))
        if latest is not None:
            status = latest.get("status", "Paused")
            next_cond = latest.get("next_condition", "unavailable")
        status_e = _esc(str(status))
        nc_e = _esc(str(next_cond))
        st_cls = _status_class(status)
        fresh = w.get("freshness", "missing")
        fresh_e = _esc(str(fresh))
        age = w.get("evidence_age_days")
        age_s = _esc(str(age)) if age is not None else "unavailable"
        thesis = _esc(str(w.get("thesis", "unavailable")))
        risk = _esc(str(w.get("risk", "unavailable")))
        entry = _esc(str(w.get("entry", "unavailable")))
        stop = _esc(str(w.get("stop", "unavailable")))
        target = _esc(str(w.get("target", "unavailable")))
        score = w.get("research_score")
        score_s = _esc(str(score)) if score is not None else "unavailable"
        evidences = w.get("evidence", [])
        ev_parts = []
        for ev in evidences:
            d = _esc(str(ev.get("date", "")))
            src = _esc(str(ev.get("source", "")))
            ex = _esc(str(ev.get("excerpt", "")))
            esha = _esc(str(ev.get("sha256", ""))[:12])
            ev_parts.append(
                f'<li>Source: {src} | date: {d} | sha: {esha}...<br><span class="ev-x">{ex}</span></li>'
            )
        ev_html = "<ul class='ev-list'>" + "".join(ev_parts) + "</ul>" if ev_parts else "<p class='muted'>No evidence records.</p>"

        # Most recent decision per selected book
        book_dec_html = []
        for bk in book_keys:
            latest = latest_decision(bk, w.get("symbol"))
            if latest is not None:
                ts = _esc(str(latest.get("timestamp", "")))
                st = _esc(str(latest.get("status", "")))
                rs = _esc(str(latest.get("reason", "")))
                nc = _esc(str(latest.get("next_condition", "")))
                ua = _esc(str(latest.get("user_action", "")))
                book_dec_html.append(
                    f"<p class='dec-line'><strong>{book_label(bk)}</strong> {ts} | status: {st}<br>reason: {rs}<br>next: {nc}<br>action: {ua}</p>"
                )
            else:
                book_dec_html.append(
                    f"<p class='dec-line'><strong>{book_label(bk)}</strong> Paused. No matching decision. Simulation only.</p>"
                )
        wl_rows.append(
            f'''<tr class="wl-row" data-symbol="{sym}" data-industry="{ind_e}" data-book="{_esc(initial_book)}">
<td><button type="button" class="sym-btn" data-symbol="{sym}" aria-expanded="false" aria-controls="detail-{sym}">{sym} / {name}</button></td>
<td>{ind_e}</td>
<td class="{st_cls}">{status_e}</td>
<td>{nc_e}</td>
</tr>
<tr class="detail-row" data-symbol="{sym}"><td colspan="4">
<div class="detail-wrap" id="detail-{sym}" hidden>
<div class="detail-grid">
<div><h4>Thesis</h4><p>{thesis}</p><h4>Risk</h4><p>{risk}</p>
<h4>Research-led simulation levels</h4><p class="muted-note">Illustrative simulation levels, not current recommendations.</p>
<p>Entry: {entry}<br>Stop: {stop}<br>Target: {target}</p>
<h4>Intraday simulation rules</h4><p>Break above the first two observations' range; fill on a later quote. Stop 3% below the simulated entry fill, target 5% above it; close at the final quote.</p>
<h4>Research score</h4><p>{score_s}</p></div>
<div><h4>Evidence</h4><p>Historical replay freshness: {fresh_e} | age at replay: {age_s} days. This is not a current-data check.</p>{ev_html}</div>
</div>
<div class="detail-dec"><h4>Most recent decision per book</h4>{"".join(book_dec_html)}</div>
<p class="ua-line">User action: <strong>No order required: simulation</strong></p>
</div>
</td></tr>'''
        )
    wl_rows_html = "".join(wl_rows) if wl_rows else '<tr><td colspan="4" class="empty">No matching tickers. No watchlist entries to display.</td></tr>'

    # Decision log rows
    log_rows = []
    for d in decisions:
        ts = _esc(str(d.get("timestamp", "")))
        bk = d.get("book", "")
        bk_e = _esc(str(bk))
        sym = _esc(str(d.get("symbol", "")))
        st = _esc(str(d.get("status", "")))
        rs = _esc(str(d.get("reason", "")))
        nc = _esc(str(d.get("next_condition", "")))
        ua = _esc(str(d.get("user_action", "")))
        log_rows.append(
            f'<tr class="log-row" data-book="{bk_e}" data-symbol="{sym}">'
            f'<td>{ts}</td><td>{bk_e}</td><td>{sym}</td><td>{st}</td>'
            f'<td>{rs}</td><td>{nc}</td><td>{ua}</td></tr>'
        )
    log_rows_html = "".join(log_rows) if log_rows else '<tr><td colspan="7" class="empty">No decisions recorded.</td></tr>'

    # Portfolios
    book_panels = []
    for k in book_keys:
        b = books.get(k, {})
        label = _esc(str(b.get("label", k)))
        cash = _fmt_money(b.get("cash"))
        init = _fmt_money(b.get("initial_cash"))
        equity = _fmt_money(b.get("equity"))
        net = _fmt_money(b.get("net_pnl"))
        fees = _fmt_money(b.get("fees"))
        slip = _fmt_money(b.get("slippage_cost"))
        realized = _fmt_money(b.get("realized_pnl"))
        rp = b.get("return_pct")
        rp_s = _fmt_pct(rp)

        positions = b.get("positions", [])
        pos_rows = []
        unreal_parts = []
        for p in positions:
            sym = _esc(str(p.get("symbol", "")))
            qty = _esc(str(p.get("quantity", "unavailable")))
            ac = _fmt_money(p.get("average_cost"), places=4)
            cb = _fmt_money(p.get("cost_basis"))
            stop = _esc(str(p.get("stop", "unavailable")))
            target = _esc(str(p.get("target", "unavailable")))
            lp = _fmt_money(p.get("last_price"), places=4)
            mt = _esc(str(p.get("mark_timestamp", "unavailable")))
            mv = _fmt_money(p.get("market_value"))
            up = p.get("unrealized_pnl")
            up_s = _fmt_money(up)
            up_dec = _to_decimal(up)
            if up_dec is not None:
                unreal_parts.append(up_dec)
            pos_rows.append(
                f'<tr><td>{sym}</td><td>{qty}</td><td>{ac}</td><td>{cb}</td>'
                f'<td>{stop}</td><td>{target}</td><td>{lp}</td><td>{mt}</td>'
                f'<td>{mv}</td><td>{up_s}</td></tr>'
            )
        pos_html = "".join(pos_rows) if pos_rows else '<tr><td colspan="10" class="empty">No open positions.</td></tr>'
        if unreal_parts:
            unreal_total = sum(unreal_parts, Decimal(0))
            unreal_s = _fmt_money(unreal_total)
        elif positions:
            unreal_s = "unavailable"
        else:
            unreal_s = _fmt_money(Decimal(0))

        events = b.get("events", [])
        ev_rows = []
        for e in events:
            eid = _esc(str(e.get("id", "")))
            ets = _esc(str(e.get("timestamp", "")))
            ebk = _esc(str(e.get("book", "")))
            esym = _esc(str(e.get("symbol", "")))
            side = _esc(str(e.get("side", "")))
            qty = _esc(str(e.get("quantity", "")))
            raw = _fmt_money(e.get("raw_price"), places=4)
            fill = _fmt_money(e.get("fill_price"), places=4)
            fee = _fmt_money(e.get("fee"))
            reason = _esc(str(e.get("reason", "")))
            ca = _fmt_money(e.get("cash_after"))
            ev_rows.append(
                f'<tr><td>{eid}</td><td>{ets}</td><td>{ebk}</td><td>{esym}</td>'
                f'<td>{side}</td><td>{qty}</td><td>{raw}</td><td>{fill}</td>'
                f'<td>{fee}</td><td>{reason}</td><td>{ca}</td></tr>'
            )
        ev_html = "".join(ev_rows) if ev_rows else '<tr><td colspan="11" class="empty">No transactions recorded.</td></tr>'

        book_panels.append(f'''<section class="book-panel" data-book="{_esc(k)}">
<h3>{label} Portfolio</h3>
<div class="stats">
<span>Initial: <b>{init}</b></span>
<span>Cash: <b>{cash}</b></span>
<span>Equity: <b>{equity}</b></span>
<span>Profit/loss: <b>{net}</b></span>
<span>Return: <b>{rp_s}</b></span>
<span>Fees: <b>{fees}</b></span>
<span>Slippage: <b>{slip}</b></span>
<span>Realized: <b>{realized}</b></span>
<span>Unrealized: <b>{unreal_s}</b></span>
</div>
<p class="muted-note">Slippage is embedded in fill price. Fees are already included in Profit/loss. Simulation only.</p>
<h4>Open positions</h4>
<div class="tw"><table><thead><tr><th>Symbol</th><th>Qty</th><th>Avg cost</th><th>Cost basis</th><th>Stop</th><th>Target</th><th>Last</th><th>Mark time</th><th>Mkt value</th><th>Unreal Profit/loss</th></tr></thead>
<tbody>{pos_html}</tbody></table></div>
<h4>Transactions</h4>
<div class="tw"><table><thead><tr><th>ID</th><th>Time</th><th>Portfolio</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Scenario price</th><th>Simulated fill</th><th>Fee</th><th>Reason</th><th>Cash after</th></tr></thead>
<tbody>{ev_html}</tbody></table></div>
<p class="muted-note">Illustrative simulation levels, not current recommendations.</p>
</section>''')
    book_panels_html = "".join(book_panels) if book_panels else '<p class="empty">No portfolio data available.</p>'

    warn_json = _safe_json(warnings)
    assump_json = _safe_json(assumptions)
    book_keys_json = _safe_json(book_keys)
    decision_index_json = _safe_json(
        {
            (bk + "|" + str(sym)): d
            for (bk, sym), ds in decision_index.items()
            for d in ds[-1:]
        }
    )

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>K2Bi / Research desk</title>
<style>
:root{{--ivory:#f7f4ec;--ink:#171514;--green:#1f5c3a;--ochre:#9c6b1e;
--rule:#d8d2c4;--muted:#6b655a;--sans:'Helvetica Neue',Helvetica,Arial,sans-serif;
--serif:Georgia,'Times New Roman',serif;--mono:'SFMono-Regular',Menlo,Consolas,monospace;}}
*{{box-sizing:border-box}}
html,body{{margin:0;padding:0;background:var(--ivory);color:var(--ink);
font-family:var(--sans);font-size:15px;line-height:1.5}}
body{{max-width:1100px;margin:0 auto;padding:0 16px 48px;width:100%}}
h1,h2,h3,h4{{font-family:var(--serif);font-weight:normal;margin:0.6em 0 0.3em}}
h1{{font-size:1.5rem;letter-spacing:0.04em}}
h2{{font-size:1.25rem;border-bottom:1px solid var(--rule);padding-bottom:0.2em}}
h3{{font-size:1.1rem}} h4{{font-size:0.95rem;color:var(--green)}}
.mono,.money{{font-family:var(--mono)}}
.muted,.muted-note,.empty{{color:var(--muted)}}
.muted-note{{font-size:0.85rem;margin:0.2em 0 0.6em}}
header.desk{{border-bottom:2px solid var(--ink);padding:14px 0 8px}}
header.desk .brand{{font-family:var(--serif);font-size:1.35rem;letter-spacing:0.06em}}
header.desk .meta{{font-family:var(--mono);font-size:0.8rem;color:var(--muted);margin-top:4px}}
.banner{{background:var(--ochre);color:#fff;padding:7px 10px;font-size:0.85rem;margin:10px 0;
border-left:4px solid #6e4a12}}
nav.tabs{{display:flex;flex-wrap:wrap;gap:0;border-bottom:1px solid var(--rule);margin:14px 0 18px}}
nav.tabs button{{background:none;border:none;border-bottom:2px solid transparent;padding:10px 16px;
font-family:var(--sans);font-size:0.95rem;color:var(--muted);cursor:pointer;letter-spacing:0.03em;
min-height:40px}}
.sym-btn{{min-height:40px}}
nav.tabs button[aria-selected="true"]{{color:var(--ink);border-bottom-color:var(--green);font-weight:600}}
nav.tabs button:focus-visible{{outline:2px solid var(--green);outline-offset:-2px}}
.panel{{display:none}} .panel.active{{display:block}}
.controls{{display:flex;flex-wrap:wrap;gap:10px;margin:0 0 14px}}
.controls label{{font-size:0.8rem;color:var(--muted);display:block}}
.controls select,.controls input[type=text]{{font-family:var(--sans);font-size:0.9rem;padding:6px 8px;
border:1px solid var(--rule);background:#fff;min-width:150px;max-width:100%}}
.controls select:focus-visible,.controls input:focus-visible{{outline:2px solid var(--green)}}
.tw{{overflow-x:auto;-webkit-overflow-scrolling:touch;border:1px solid var(--rule);background:#fff}}
table{{border-collapse:collapse;width:100%;min-width:640px;font-size:0.88rem}}
th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid var(--rule);vertical-align:top}}
th{{font-family:var(--sans);font-size:0.75rem;text-transform:uppercase;letter-spacing:0.05em;
color:var(--muted);background:#f1ecdf;position:sticky;top:0}}
td.money{{font-family:var(--mono)}}
.sym-btn{{background:none;border:1px solid var(--rule);padding:5px 9px;font-family:var(--sans);
font-size:0.88rem;color:var(--ink);cursor:pointer;text-align:left;border-radius:2px}}
.sym-btn:hover{{border-color:var(--green);color:var(--green)}}
.sym-btn:focus-visible{{outline:2px solid var(--green);outline-offset:1px}}
.st-fresh{{color:var(--green);font-weight:600}}
.st-paused{{color:var(--ochre);font-weight:600}}
.st-missing{{color:#8a2f2f;font-weight:600}}
.st-neutral{{color:var(--ink)}}
.st-fresh::before{{content:"\\25CF ";font-size:0.7em}}
.st-paused::before{{content:"\\25CB ";font-size:0.7em}}
.st-missing::before{{content:"\\25A0 ";font-size:0.7em}}
tr.wl-row td{{border-bottom:none}}
tr.detail-row td{{border-bottom:1px solid var(--rule);padding:0}}
.detail-wrap{{padding:14px 16px;background:#fbf9f3;border-left:3px solid var(--green)}}
.detail-grid{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}
.detail-grid p{{margin:0.2em 0 0.8em}}
.ev-list{{margin:0;padding-left:18px;font-size:0.85rem}}
.ev-list li{{margin-bottom:0.5em}}
.ev-x,.detail-wrap p,.detail-wrap li,.dec-line,.meta,.muted-note{{overflow-wrap:anywhere;word-break:break-word}}
.ev-x{{color:var(--muted);font-style:italic}}
.dec-line{{font-size:0.85rem;margin:0.4em 0;padding-left:10px;border-left:2px solid var(--rule)}}
.ua-line{{margin-top:12px;font-size:0.9rem}}
.book-panel{{border-top:1px solid var(--rule);padding-top:12px;margin-top:22px}}
.stats{{display:flex;flex-wrap:wrap;gap:8px 20px;font-size:0.88rem;margin:8px 0}}
.stats b{{font-family:var(--mono);font-weight:600}}
details{{margin:12px 0;border:1px solid var(--rule);background:#fff;padding:0 12px}}
details summary{{cursor:pointer;padding:10px 0;font-weight:600;font-size:0.9rem}}
details summary:focus-visible{{outline:2px solid var(--green)}}
details ul{{margin:0 0 12px;padding-left:20px;font-size:0.88rem}}
.countline{{font-family:var(--mono);font-size:0.82rem;color:var(--muted);margin:8px 0}}
.filterline{{font-size:0.85rem;margin:8px 0;color:var(--muted)}}
@media (max-width:640px){{
body{{padding:0 10px 40px;max-width:100%}}
.detail-grid{{grid-template-columns:1fr}}
h1{{font-size:1.25rem}}
nav.tabs button{{padding:10px 10px;font-size:0.9rem;min-height:40px}}
.controls{{gap:8px}}
.controls>div{{flex:1 1 100%}}
.controls select,.controls input{{min-width:0;width:100%;min-height:40px}}
.sym-btn{{min-height:40px;width:100%}}
}}
@media (prefers-reduced-motion:reduce){{*{{transition:none!important;animation:none!important}}}}
</style></head>
<body>
<header class="desk">
<div class="brand">K2Bi / Research desk</div>
<div class="meta">As of {as_of} | mode: {mode}</div>
</header>
<div class="banner" role="status">Historical research + synthetic prices. Simulation only. No current buy/sell recommendation.</div>
<noscript><p class="banner" style="background:#8a2f2f">JavaScript is required for interactive filters and detail views. Static summary shown below.</p></noscript>

<nav class="tabs" role="tablist" aria-label="Desk sections">
<button type="button" role="tab" aria-selected="true" aria-controls="panel-watchlist" id="tab-watchlist">Watchlist</button>
<button type="button" role="tab" aria-selected="false" aria-controls="panel-portfolios" id="tab-portfolios">Portfolios</button>
<button type="button" role="tab" aria-selected="false" aria-controls="panel-log" id="tab-log">Decision log</button>
</nav>

<section class="panel active" id="panel-watchlist" role="tabpanel" aria-labelledby="tab-watchlist">
<h2>Watchlist</h2>
<div class="controls">
<div><label for="f-industry">Industry</label>
<select id="f-industry">{''.join(ind_options)}</select></div>
<div><label for="f-search">Search ticker or company</label>
<input type="text" id="f-search" placeholder="e.g. G or CALX" autocomplete="off"></div>
<div><label for="f-book">Portfolio</label>
<select id="f-book">{''.join(wl_book_options)}</select></div>
</div>
<p class="countline" aria-live="polite" id="wl-count">Showing {len(watchlist)} of {len(watchlist)} rows</p>
<p class="filterline" id="wl-help" hidden>No matching tickers. Try clearing the search or choosing another portfolio.</p>
<div class="tw"><table id="wl-table">
<thead><tr><th>Ticker / company</th><th>Industry</th><th>Status</th><th>Next condition</th></tr></thead>
<tbody>{wl_rows_html}</tbody>
</table></div>
</section>

<section class="panel" id="panel-portfolios" role="tabpanel" aria-labelledby="tab-portfolios" hidden>
<h2>Portfolios</h2>
<p class="muted-note">Both books shown. Simulation only. Unknown values are marked unavailable, never zero.</p>
{book_panels_html}
</section>

<section class="panel" id="panel-log" role="tabpanel" aria-labelledby="tab-log" hidden>
<h2>Decision log</h2>
<div class="controls">
<div><label for="log-book">Portfolio filter</label>
<select id="log-book">{''.join(log_book_options)}</select></div>
<div><label for="log-search">Search symbol or reason</label>
<input type="text" id="log-search" placeholder="e.g. ACME or risk" autocomplete="off"></div>
</div>
<p class="countline" aria-live="polite" id="log-count">Showing {len(decisions)} of {len(decisions)} decisions</p>
<div class="tw"><table id="log-table">
<thead><tr><th>Time</th><th>Book</th><th>Ticker</th><th>Status</th><th>Reason</th><th>Next condition</th><th>User action</th></tr></thead>
<tbody>{log_rows_html}</tbody>
</table></div>
</section>

<details>
<summary>Assumptions and warnings</summary>
<p class="muted-note">Illustrative simulation levels, not current recommendations.</p>
<div id="assump-block"></div>
<div id="warn-block"></div>
</details>

<script>
(function(){{
"use strict";
var ASSUMPTIONS={assump_json};
var WARNINGS={warn_json};
var BOOK_KEYS={book_keys_json};
var DECISIONS={decision_index_json};
function lookup(book,symbol){{
if(!book||symbol==null)return null;
var key=String(book)+"|"+String(symbol);
return Object.prototype.hasOwnProperty.call(DECISIONS,key)?DECISIONS[key]:null;
}}
function el(tag,cls,txt){{var e=document.createElement(tag);if(cls)e.className=cls;if(txt!=null)e.textContent=String(txt);return e;}}
function fillList(containerId,title,items){{
var host=document.getElementById(containerId);
host.textContent="";
host.appendChild(el("h4",null,title));
if(!items||!items.length){{host.appendChild(el("p","muted","None recorded."));return;}}
var ul=document.createElement("ul");
for(var i=0;i<items.length;i++){{ul.appendChild(el("li",null,items[i]));}}
host.appendChild(ul);
}}
fillList("assump-block","Assumptions",ASSUMPTIONS);
fillList("warn-block","Warnings",WARNINGS);

/* Tabs */
var tabs=[
{{btn:document.getElementById("tab-watchlist"),panel:document.getElementById("panel-watchlist")}},
{{btn:document.getElementById("tab-portfolios"),panel:document.getElementById("panel-portfolios")}},
{{btn:document.getElementById("tab-log"),panel:document.getElementById("panel-log")}}
];
tabs.forEach(function(t){{
t.btn.addEventListener("click",function(){{
tabs.forEach(function(o){{o.btn.setAttribute("aria-selected","false");o.panel.hidden=true;o.panel.classList.remove("active");}});
t.btn.setAttribute("aria-selected","true");t.panel.hidden=false;t.panel.classList.add("active");
}});
}});

/* Watchlist filters */
var fInd=document.getElementById("f-industry"),fSearch=document.getElementById("f-search"),
fBook=document.getElementById("f-book"),wlCount=document.getElementById("wl-count"),
wlHelp=document.getElementById("wl-help");
var wlRows=Array.prototype.slice.call(document.querySelectorAll("tr.wl-row"));
function rowVisible(r){{
var ind=fInd.value,term=(fSearch.value||"").toLowerCase().trim();
var okInd=(ind==="all")||(r.getAttribute("data-industry")===ind);
var text=(r.textContent||"").toLowerCase();
var okTerm=!term||text.indexOf(term)!==-1;
return okInd&&okTerm;
}}
function applyWl(){{
var shown=0;
wlRows.forEach(function(r){{
var show=rowVisible(r);
r.style.display=show?"":"none";
var det=document.querySelector('tr.detail-row[data-symbol="'+r.getAttribute("data-symbol")+'"]');
if(det){{
if(show){{det.style.display="";}}
else{{det.style.display="none";
var wrap=det.querySelector(".detail-wrap");
if(wrap)wrap.hidden=true;
var btn=document.querySelector('.sym-btn[data-symbol="'+r.getAttribute("data-symbol")+'"]');
if(btn)btn.setAttribute("aria-expanded","false");
}}
}}
if(show)shown++;
}});
wlCount.textContent="Showing "+shown+" of "+wlRows.length+" rows";
wlHelp.hidden=(shown!==0)&&wlRows.length>0;
}}
fInd.addEventListener("change",applyWl);
fSearch.addEventListener("input",applyWl);
function applyBook(){{
var bk=fBook.value;
wlRows.forEach(function(r){{
var d=lookup(bk,r.getAttribute("data-symbol"));
var statusCell=r.children[2],nextCell=r.children[3];
if(d){{
statusCell.textContent=d.status||"Paused";
statusCell.className=(function(s){{
s=String(s||"").toLowerCase();
if(s.indexOf("paused")!==-1||s.indexOf("stale")!==-1)return "st-paused";
if(s.indexOf("fresh")!==-1||s.indexOf("active")!==-1||s.indexOf("clear")!==-1)return "st-fresh";
if(s.indexOf("missing")!==-1||s.indexOf("blocked")!==-1||s.indexOf("risk")!==-1)return "st-missing";
return "st-neutral";
}})(d.status||"");
nextCell.textContent=d.next_condition||"unavailable";
}}else{{
statusCell.textContent="Paused";
statusCell.className="st-neutral";
nextCell.textContent="unavailable";
}}
}});
applyWl();
}}
fBook.addEventListener("change",applyBook);

/* Detail toggles */
var symBtns=Array.prototype.slice.call(document.querySelectorAll(".sym-btn"));
symBtns.forEach(function(b){{
b.addEventListener("click",function(){{
var sym=b.getAttribute("data-symbol");
var w=document.getElementById("detail-"+sym);
if(!w)return;
var row=document.querySelector('tr.wl-row[data-symbol="'+sym+'"]');
if(row&&row.style.display==="none")return;
var open=!w.hidden;
w.hidden=open;
b.setAttribute("aria-expanded",open?"false":"true");
}});
b.setAttribute("aria-expanded","false");
}});

/* Decision log filters */
var logBook=document.getElementById("log-book"),logSearch=document.getElementById("log-search"),
logCount=document.getElementById("log-count");
var logRows=Array.prototype.slice.call(document.querySelectorAll("tr.log-row"));
function applyLog(){{
var bk=logBook.value,term=(logSearch.value||"").toLowerCase().trim(),shown=0;
logRows.forEach(function(r){{
var okBk=(r.getAttribute("data-book")===bk);
var text=(r.textContent||"").toLowerCase();
var okTerm=!term||text.indexOf(term)!==-1;
var show=okBk&&okTerm;
r.style.display=show?"":"none";
if(show)shown++;
}});
logCount.textContent="Showing "+shown+" of "+logRows.length+" decisions";
}}
logBook.addEventListener("change",function(){{
applyLog();
if(fBook&&fBook.value!==logBook.value){{fBook.value=logBook.value;applyBook();}}
}});
logSearch.addEventListener("input",applyLog);
applyLog();
if(fBook&&logBook.value!==fBook.value){{
logBook.value=fBook.value;
applyLog();
}}
if(fBook){{fBook.addEventListener("change",function(){{if(logBook.value!==fBook.value){{logBook.value=fBook.value;applyLog();}}}});}}
applyWl();
}})();
</script>
</body></html>"""
    return html
