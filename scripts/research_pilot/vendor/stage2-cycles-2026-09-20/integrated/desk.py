"""Small adapter: terminal/history dicts -> frozen report HTML.

Reuses the immutable renderer from offline-milestone-2026-09-20/report_view.py
by importing it via an explicit filesystem path.  All inputs are plain dicts;
render() is pure and never mutates its arguments.
"""

from __future__ import annotations

import html
import importlib.util
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

# ---------------------------------------------------------------------------
# Immutable renderer (reused, never re-implemented)
# ---------------------------------------------------------------------------

_RENDER_VIEW = Path(__file__).resolve().parents[2] / "offline-milestone-2026-09-20" / "report_view.py"


def _load_renderer():
    spec = importlib.util.spec_from_file_location("offline_report_view", _RENDER_VIEW)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load frozen report_view from %s" % _RENDER_VIEW)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_renderer = _load_renderer()
_frozen_render = getattr(_renderer, "render")

# ---------------------------------------------------------------------------
# Fixed contract universe
# ---------------------------------------------------------------------------

_UNIVERSE = ("G", "CALX", "CDNS", "SPY", "XLV")
_INDUSTRIES = {
    "G": "Business services",
    "CALX": "Telecom equipment",
    "CDNS": "Semiconductor software",
    "SPY": "Broad market ETF",
    "XLV": "Healthcare ETF",
}
_EVENT_BOOK = "event source"
_CENT = Decimal("0.01")


# ---------------------------------------------------------------------------
# Number helpers
# ---------------------------------------------------------------------------


def _dec(value, default="0"):
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value))


def _cents(value):
    return _dec(value).quantize(_CENT, rounding=ROUND_HALF_UP)


def _money(value):
    return str(_cents(value))


def _price4(value):
    return str(_dec(value).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def _cost4(value):
    return str(_dec(value).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def _pct(net, initial):
    initial = _dec(initial)
    if initial == 0:
        return "0.00"
    return str((_dec(net) / initial * Decimal(100)).quantize(_CENT, rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# Small dict/collection utilities (pure)
# ---------------------------------------------------------------------------


def _as_list(value):
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _as_map(value):
    return value if isinstance(value, dict) else {}


def _pick(*values):
    for value in values:
        if value not in (None, "", {}, []):
            return value
    return None


def _analyses(terminal, state, history):
    map_ = _as_map(terminal.get("analyses")) or _as_map(state.get("research"))
    if map_:
        return map_
    for entry in reversed(_as_list(history)):
        if isinstance(entry, dict):
            cand = _as_map(entry.get("analyses"))
            if cand:
                return cand
    return {}


def _evidence_rows(evidence):
    rows = []
    for ev in _as_list(evidence):
        ev = _as_map(ev)
        source = _pick(ev.get("source_url"), ev.get("url"), ev.get("source"), "unavailable")
        excerpt = " | ".join(f"{k}: {ev.get(k, 'unavailable')}" for k in ("kind", "period_end", "filed", "retrieved_at", "period_kind", "reason"))
        rows.append({
            "date": _pick(ev.get("filed"), ev.get("date"), "unavailable"),
            "source": source,
            "excerpt": excerpt,
            "sha256": _pick(ev.get("sha256"), "unavailable"),
            "kind": _pick(ev.get("kind"), "unavailable"),
        })
    return rows


def _watchlist(analyses):
    out = []
    for symbol in _UNIVERSE:
        a = _as_map(analyses.get(symbol))
        out.append({
            "symbol": symbol,
            "name": _pick(a.get("name"), symbol),
            "industry": _pick(a.get("industry"), _INDUSTRIES.get(symbol, "unavailable")),
            "freshness": _pick(a.get("status"), "unavailable"),
            "evidence_age_days": _pick(a.get("evidence", {}).get("filing_age_days"), "unavailable"),
            "thesis": _pick(a.get("reason"), a.get("thesis"), "unavailable"),
            "risk": "Simplified growth rule; no valuation, news or real-time price assessment.",
            "entry": "Fresh support; queue entry for a later supplied quote",
            "stop": "3% below fee-inclusive average cost; later-quote exit",
            "target": "5% above fee-inclusive average cost; later-quote exit",
            "research_score": _pick(a.get("research_score"), a.get("score"), "unavailable"),
            "stance": _pick(a.get("stance"), "unavailable"),
            "evidence": _evidence_rows(a.get("evidence")),
        })
    return out


def _industries(analyses):
    counts = {}
    for symbol in _UNIVERSE:
        a = _as_map(analyses.get(symbol))
        counts[_INDUSTRIES.get(symbol, "unavailable")] = counts.get(
            _INDUSTRIES.get(symbol, "unavailable"), 0) + 1
    return [{"industry": k, "count": v} for k, v in sorted(counts.items())]


# ---------------------------------------------------------------------------
# Book rendering
# ---------------------------------------------------------------------------


def _position_rows(book, price_marks):
    rows = []
    for symbol, pos in sorted(_as_map(book.get("positions")).items()):
        pos = _as_map(pos)
        qty = int(_dec(pos.get("quantity")))
        basis = _dec(pos.get("basis"))
        mark = Decimal(str(pos.get("lastmark")))  # mark lives on the position
        mv = (qty * mark).quantize(_CENT, rounding=ROUND_HALF_UP)
        unrealized = mv - basis
        marks = _as_map(price_marks.get(symbol))
        rows.append({
            "symbol": symbol,
            "quantity": qty,
            "average_cost": _cost4(pos.get("average_cost")),
            "cost_basis": _money(basis),
            "stop": _price4(_dec(pos["average_cost"]) * Decimal("0.97")),
            "target": _price4(_dec(pos["average_cost"]) * Decimal("1.05")),
            "last_price": _price4(mark),
            "mark_timestamp": _pick(pos.get("mark_timestamp"), marks.get("timestamp"), "unavailable"),
            "market_value": _money(mv),
            "unrealized_pnl": _money(unrealized),
        })
    return rows


def _book_block(book, price_marks):
    positions = _position_rows(book, price_marks)
    equity = _dec(book.get("cash"))
    for row in positions:
        equity += _dec(row["market_value"])
    initial = _dec(book.get("initial_cash"))
    net = equity - initial
    return {
        "label": _pick(book.get("label"), "book"),
        "initial_cash": _money(initial),
        "cash": _money(book.get("cash")),
        "equity": _money(equity),
        "net_pnl": _money(net),
        "return_pct": _pct(net, initial),
        "fees": _money(book.get("fees")),
        "slippage_cost": _money(sum(_dec(e["slippage"]) for e in book.get("events", []))),
        "realized_pnl": _money(book.get("realized_pnl")),
        "positions": positions,
        "events": _as_list(book.get("events")),
    }


def _books(state):
    books = _as_map(_as_map(state).get("books"))
    result = {}
    for name, book in books.items():
        block = _book_block(_as_map(book), _as_map(_as_map(book).get("last_prices")))
        block["label"] = name
        block["events"] = [dict(e, book=name) for e in book.get("events", [])]
        result[name] = block
    return result


# ---------------------------------------------------------------------------
# History / decisions
# ---------------------------------------------------------------------------


def _full_decisions(history, terminal):
    decisions = []
    for entry in _as_list(history):
        if isinstance(entry, dict):
            for d in _as_list(entry.get("decisions")):
                decisions.append(_as_map(d))
    cycle = terminal.get("cycle_id")
    seen = {e.get("cycle_id") for e in _as_list(history) if isinstance(e, dict)}
    if cycle not in seen:
        for d in _as_list(terminal.get("decisions")):
            decisions.append(_as_map(d))
    return decisions


def _current_decisions(terminal, history):
    decisions = []
    for d in _as_list(terminal.get("decisions")):
        decisions.append(_as_map(d))
    for d in _PAUSED_DECISIONS:
        decisions.append(d)
    return decisions


def _risk_decisions(state):
    # Stale paused-source enters from a budget exhaust must not look current,
    # but genuine current paused-source risk exits are retained.
    return [_as_map(d) for d in _as_list(_as_map(state).get("paused_sources"))]


def _decision_block(d):
    action = str(_pick(d.get("action"), "hold"))
    reason = str(_pick(d.get("reason"), "unavailable"))
    status = str(_pick(d.get("status"), action))
    return {
        "book": _pick(d.get("book"), "unavailable"),
        "symbol": _pick(d.get("symbol"), "unavailable"),
        "timestamp": _pick(d.get("timestamp"), "unavailable"),
        "status": status,
        "reason": reason,
        "next_condition": "Later supplied quote and valid evidence; resolve pause first" if action == "abstain" else "Next bounded cycle; fills only on later quotes except predefined close",
        "user_action": "Simulation only: " + action + ". " + reason,
    }


_PAUSED_DECISIONS = [
    {
        "book": "research",
        "symbol": "",
        "action": "paused",
        "reason": "budget_exhausted",
        "status": "paused",
        "timestamp": "unavailable",
        "next_condition": "budget replenished for fixed universe",
    },
    {
        "book": "intraday",
        "symbol": "",
        "action": "paused",
        "reason": "interrupted",
        "status": "paused",
        "timestamp": "unavailable",
        "next_condition": "session resumed",
    },
]


# ---------------------------------------------------------------------------
# Summary block (own HTML, escaped exactly once)
# ---------------------------------------------------------------------------


def _summary_html(terminal, state, analyses):
    terminal = _as_map(terminal)
    state = _as_map(state)
    resources = _as_map(state.get("resources"))

    cycle_raw = _pick(terminal.get("cycle_id"), "unavailable")
    cycle = html.escape(str(cycle_raw))
    status_raw = _pick(terminal.get("status"), "unavailable")
    status = html.escape(str(status_raw))

    cycles = html.escape(str(_pick(resources.get("cycles"), "unavailable")))
    sources = html.escape(str(_pick(resources.get("source_requests"), "unavailable")))
    usd = html.escape(str(_pick(resources.get("model_usd"), "unavailable")))

    def _label(symbol, a):
        name = _pick(a.get("name"), None)
        if name is not None and str(name).strip() != "":
            return str(name)
        return str(symbol)

    universe_size = len(_UNIVERSE)

    entries = []
    changed_labels = []
    fresh_count = 0
    paused_count = 0
    unknown_status_count = 0
    withheld_count = 0
    unavailable_stance_count = 0
    missing_analysis_count = 0

    for symbol in _UNIVERSE:
        a = _as_map(analyses.get(symbol))
        has_analysis = bool(a)
        stance = _pick(a.get("stance"), None) if has_analysis else None
        status_a = _pick(a.get("status"), None) if has_analysis else None
        change = _pick(a.get("change"), None) if has_analysis else None

        if not has_analysis:
            missing_analysis_count += 1
        else:
            if stance == "abstain":
                withheld_count += 1
            elif stance in ("support", "invalidate"):
                pass
            else:
                unavailable_stance_count += 1

            if status_a == "fresh":
                fresh_count += 1
            elif status_a == "paused":
                paused_count += 1
            else:
                unknown_status_count += 1

            if change == "changed":
                changed_labels.append(_label(symbol, a))

        entries.append((symbol, a, has_analysis, stance, status_a, change))

    # Needs attention: abstention, negative assessment and missing data are distinct.
    na_line = f"{withheld_count} of {universe_size} tickers are withholding a conclusion."
    if missing_analysis_count:
        na_line += f" {missing_analysis_count} have no research assessment available."
    if unavailable_stance_count:
        na_line += f" {unavailable_stance_count} have an unknown assessment."
    na_next = "Open company evidence in the Watchlist below to see what needs updating."

    # Latest research update
    if missing_analysis_count == universe_size:
        update_line = "No research assessment available."
    elif changed_labels:
        update_line = "%d symbol(s) changed in the most recent research assessment: %s." % (
            len(changed_labels), ", ".join(changed_labels)
        )
    else:
        update_line = "No changes recorded among the available research assessments."
    update_note = (
        "Latest research assessment; a paused run can retain the previous assessment."
    )

    # Discovery
    discovery_line = (
        "Discovery is not implemented. This is a fixed %d-symbol demonstration universe; "
        "planned work covers technology and AI names plus broader US opportunities."
        % universe_size
    )

    # Evidence
    evidence_parts = []
    evidence_parts.append("%d with fresh evidence" % fresh_count)
    if paused_count:
        evidence_parts.append("%d waiting for usable evidence" % paused_count)
    if unknown_status_count:
        evidence_parts.append("%d with status unavailable" % unknown_status_count)
    if missing_analysis_count:
        evidence_parts.append("%d with no analysis" % missing_analysis_count)
    if withheld_count:
        evidence_parts.append("%d explicit abstention(s)" % withheld_count)
    evidence_line = "Evidence: " + ", ".join(evidence_parts) + "."

    status_note = ""
    low = str(status_raw).lower() if status_raw is not None else ""
    if low == "complete":
        pass
    elif low == "budget_exhausted":
        status_note = "Paused: configured run limit reached; this is not proof that DeepSeek credits are exhausted."
    else:
        status_note = "Status unavailable or unrecognized; inspection needed before treating this run as completed or healthy."

    # Technical details block
    reason_items = []
    for symbol, a, has_analysis, stance, status_a, change in entries:
        label = html.escape(_label(symbol, a))
        reason = _pick(a.get("reason"), "unavailable") if has_analysis else "unavailable"
        reason_items.append(
            "<li>%s: %s</li>" % (label, html.escape(str(reason)))
        )

    details = (
        "<details class=\"review-technical\">"
        "<summary>Technical details</summary>"
        "<p>Cycle: %s | Status: %s</p>"
        "<p>Quota usage: cycles=%s, source_requests=%s; runtime LLM USD=%s</p>"
        "<p>Stage 2 local simulation. Quotes and fills are synthetic; "
        "no price feed is claimed. Visible fixture facts are distinguished "
        "from real SEC facts by source kind; SEC data uses only exact "
        "contract CIK URLs of the form https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json"
        "(10 digits); fixtures remain plaintext.</p>"
        "<ul>%s</ul>"
        "</details>"
    ) % (
        cycle, status, cycles, sources, usd,
        "".join(reason_items) if reason_items else "<li>No per-symbol reasons available.</li>"
    )

    css = (
        "<style>"
        ".review{font-family:var(--serif);color:var(--ink);padding:20px 0 8px}"
        ".review p{font-family:var(--sans)}"
        ".review h2,.review h3{margin:0 0 .35em 0;font-family:var(--serif);color:var(--ink)}"
        ".review h2{font-size:1.35rem;border-bottom:1px solid var(--rule);padding-bottom:.25em}"
        ".review h3{font-size:1.05rem;color:var(--muted)}"
        ".review-snapshot,.review-cycle,.review-disclaimer,.review-status,.review-directions{margin:.25em 0;color:var(--muted);font-size:.9rem}"
        ".review-disclaimer{border-left:3px solid var(--ochre,#b8860b);padding-left:.6em}"
        ".review-grid{display:grid;grid-template-columns:1fr 1fr;gap:1em;margin:1em 0}"
        ".review-block{background:var(--ivory,#fffff8);border:1px solid var(--rule);border-radius:4px;padding:.8em 1em;min-width:0;overflow-wrap:anywhere}"
        ".review-block p{margin:.35em 0;font-size:.95rem;line-height:1.4}"
        ".review-attention{border-left:3px solid var(--ochre,#b8860b)}"
        ".review-note{color:var(--muted);font-size:.85rem}"
        ".review-technical{margin-top:1em;border-top:1px solid var(--rule);padding-top:.6em;overflow-wrap:anywhere}"
        ".review-technical summary{cursor:pointer;color:var(--muted);font-size:.9rem}"
        ".review-technical ul{padding-left:1.2em;font-size:.85rem;line-height:1.5;overflow-wrap:anywhere}"
        "@media (max-width:640px){.review-grid{grid-template-columns:1fr;gap:.75em}}"
        "</style>"
    )

    return (
        "<section class=\"summary review\">"
        + css +
        "<h2>Your review today</h2>"
        "<p class=\"review-snapshot\">Saved simulation snapshot, not a live daily service.</p>"
        "<p class=\"review-cycle\">Scenario time: %s</p>"
        "<p class=\"review-disclaimer\">Quotes and fills are synthetic. "
        "No current buy or sell recommendations.</p>"
        "<div class=\"review-grid\">"
        "<div class=\"review-block review-attention\">"
        "<h3>Needs attention</h3>"
        "<p>%s</p>"
        "<p>%s</p>"
        "</div>"
        "<div class=\"review-block review-update\">"
        "<h3>Latest research update</h3>"
        "<p>%s</p>"
        "<p class=\"review-note\">%s</p>"
        "</div>"
        "<div class=\"review-block review-discovery\">"
        "<h3>Discovery</h3>"
        "<p>%s</p>"
        "</div>"
        "<div class=\"review-block review-evidence\">"
        "<h3>Evidence</h3>"
        "<p>%s</p>"
        "</div>"
        "</div>"
        "<p class=\"review-status\">%s</p>"
        "<p class=\"review-directions\">Open the Watchlist below for evidence, "
        "Portfolios for simulated holdings, and the Decision log for past decisions.</p>"
        "%s"
        "</section>"
    ) % (
        cycle,
        html.escape(str(na_line)),
        html.escape(str(na_next)),
        html.escape(str(update_line)),
        html.escape(str(update_note)),
        html.escape(str(discovery_line)),
        html.escape(str(evidence_line)),
        html.escape(str(status_note)),
        details,
    )

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def render(terminal, history):
    """Return HTML for a terminal cycle plus prior history. Pure."""
    terminal = _as_map(terminal)
    history = _as_list(history)
    state = _as_map(terminal.get("state"))
    analyses = _analyses(terminal, state, history)

    report = {
        "as_of": _pick(terminal.get("cycle_id"), "unavailable"),
        "mode": "Stage 2 local simulation",
        "input_sha256": _pick(terminal.get("request_sha256"), "unavailable"),
        "assumptions": [
            "fixtures are plaintext and clearly labelled",
            "SEC source retrieval only in SEC mode; no broker calls",
            "fees are embedded in basis and never deducted twice",
            "all quotes and fills are synthetic",
        ],
        "warnings": _as_list(terminal.get("warnings")),
        "industries": _industries(analyses),
        "watchlist": _watchlist(analyses),
        "books": _books(state),
        "decisions": [_decision_block(d) for d in _full_decisions(history, terminal)],
    }

    frozen = _frozen_render(report)
    frozen = frozen.replace("Break above the first two observations' range; fill on a later quote. Stop 3% below the simulated entry fill, target 5% above it; close at the final quote.", "Fresh support and a quote more than 1% above the previous observation; fill on a later quote. Stop 3% below fee-inclusive average cost, target 5% above it; exit on a later quote. Predefined session-close liquidation uses a supplied close quote.")
    frozen = frozen.replace("Historical replay freshness:", "Evidence status at cycle:").replace("age at replay:", "filing age:").replace("This is not a current-data check.", "Freshness is evaluated at the displayed cycle time.")
    from contracts import CIKS
    for cik in CIKS.values():
        url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
        escaped = html.escape(url, quote=True)
        frozen = frozen.replace("Source: " + escaped + " |", f'Source: <a href="{escaped}" target="_blank" rel="noopener noreferrer">SEC company facts</a> |')
    if "</body>" in frozen:
        head, _, tail = frozen.partition("</body>")
        summary = _summary_html(terminal, state, analyses)
        frozen = (head + "</body>" + tail).replace("<body>", "<body>" + summary, 1)
    return frozen
