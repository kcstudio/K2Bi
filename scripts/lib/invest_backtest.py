"""invest-backtest -- Bundle 4 cycle 3 (m2.15).

yfinance 2-year sanity-check backtest for a K2Bi strategy spec. Explicitly
NOT walk-forward (walk-forward harness is Phase 4 conditional per
`milestones.md` Phase 4.2 trigger). Phase 2 MVP only.

Architecture (per spec §3.5):

    Claude (SKILL.md body)
        -> /backtest <slug>
        -> invokes scripts.lib.invest_backtest.run_backtest(slug, ...)
    run_backtest (this module)
        -> read strategy file via scripts.lib.strategy_frontmatter
        -> extract order.ticker (Phase 2 MVP: one primary symbol per strategy)
        -> pull 2y daily yfinance bars for symbol + SPY (reference)
        -> vectorized pandas sim: lag-1 SMA(20)/SMA(50) crossover baseline
        -> compute metrics: sharpe, sortino, max_dd, win_rate, avg_winner,
           avg_loser, total_return, n_trades, avg_trade_holding_days
        -> sanity gate: total_return > 500 OR max_dd > -2 OR win_rate > 85
           trips look_ahead_check: suspicious (file is written REGARDLESS;
           approval gate is the refusal layer -- this module only audits)
        -> atomic write raw/backtests/<date>_<slug>_backtest.md via
           strategy_frontmatter.atomic_write_bytes
    Claude (SKILL.md body, post-return)
        -> appends wiki/log.md entry via scripts/wiki-log-append.sh
        -> surfaces BacktestResult to Keith

Key spec refs:

    §2.5 -- LOCKED output schema + filename + atomic-write policy
    §3.5 -- LOCKED scan_backtests_for_slug algorithm (consumer side)

Check D protection: the strategy file at `wiki/strategies/strategy_<slug>.md`
is NEVER touched by this module. Metrics land in an immutable per-run capture
in `raw/backtests/`, outside Check D's `wiki/strategies/strategy_*.md` glob.

Why SMA-crossover baseline: K2Bi strategy specs don't carry structured
entry/exit rules in Phase 2 (the prose lives in `## How This Works`). A
fixed deterministic baseline on the strategy's primary symbol is what the
Phase 2 sanity gate actually audits -- it catches look-ahead bugs in the
data pipeline (gaps, reindexing, future-fill) and trivially-unrealistic
claims (2y win rates >85% on equity). Phase 4's walk-forward harness
replaces this with real strategy-rule extraction when a second strategy
or overfit signal triggers it.
"""

from __future__ import annotations

import datetime as _dt
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from scripts.lib import strategy_frontmatter as sf


# ---------- constants ----------


# Spec §2.5 sanity gate thresholds. Tripping any one yields
# look_ahead_check: suspicious with a reason naming the tripped metric(s).
# Thresholds picked to catch the "11,000% P&L look-ahead cheat" family from
# retail-trading research (>500% 2y return on equity, ~0% drawdown on
# anything resembling a real strategy, win rates implausible without
# look-ahead). Phase 4 may add more thresholds; Phase 2 locks these three.
TOTAL_RETURN_PCT_THRESHOLD = 500.0
MAX_DD_PCT_THRESHOLD = -2.0  # max_dd_pct is NEGATIVE; suspicious when > -2
WIN_RATE_PCT_THRESHOLD = 85.0

# Phase 2 MVP baseline windows (long-only SMA crossover). Short + long
# period picked for ~9-day average holding time on SPY which matches the
# spec §2.5 example shape; phase 4 replaces this with rule-extraction.
SMA_SHORT = 20
SMA_LONG = 50

# Trading-day count for annualization (Sharpe / Sortino). 252 is US-equity
# convention; matches yfinance's daily-bar cadence which skips weekends +
# US market holidays natively.
TRADING_DAYS_PER_YEAR = 252

# 2-year default window. 730 days ≈ 2 calendar years; yfinance snaps to
# nearest trading bars. Phase 2 MVP locks this; overrides supported via
# run_backtest(window_start=..., window_end=...) kwargs.
DEFAULT_WINDOW_DAYS = 730


# ---------- exceptions ----------


class BacktestError(ValueError):
    """Any validation / data-pull / simulation failure. Message surfaces
    directly to Keith via SKILL.md `echo` + `exit 1` path."""


# ---------- dataclasses ----------


@dataclass(frozen=True)
class BacktestWindow:
    """Concrete date window used for the backtest. Spec §2.5 nests under
    `backtest.window`. Inclusive on both ends; yfinance filters to actual
    trading days within this range."""

    start: _dt.date
    end: _dt.date


@dataclass(frozen=True)
class BacktestMetrics:
    """Per-spec-§2.5 metrics block. All percentages are in PERCENT (not
    fractional); `sharpe` + `sortino` are annualised. `max_dd_pct` is a
    NEGATIVE number (peak-to-trough drawdown); the sanity gate treats any
    value greater than -2 (i.e. too-shallow drawdown) as suspicious."""

    sharpe: float
    sortino: float
    max_dd_pct: float
    win_rate_pct: float
    avg_winner_pct: float
    avg_loser_pct: float
    total_return_pct: float
    n_trades: int
    avg_trade_holding_days: float


@dataclass(frozen=True)
class BacktestEntry:
    """One round-trip trade. Not persisted into the capture body for
    Phase 2 MVP (the body carries a summary table, not per-trade rows);
    kept in the result for downstream analysis + test assertions."""

    entry_date: _dt.date
    exit_date: _dt.date
    entry_price: float
    exit_price: float
    return_pct: float
    holding_days: int


@dataclass(frozen=True)
class BacktestResult:
    """run_backtest return shape. `path` is the file written under
    `raw/backtests/`; callers (SKILL.md body) use this to log via
    wiki-log-append.sh without re-deriving the filename."""

    path: Path
    slug: str
    symbol: str
    reference_symbol: Optional[str]
    window: BacktestWindow
    metrics: BacktestMetrics
    look_ahead_check: str
    look_ahead_check_reason: str
    last_run: _dt.datetime
    strategy_commit_sha: str
    source_version: str
    trades: list[BacktestEntry]


# Function signature for a price fetcher. Tests inject a deterministic
# fixture; production defaults to `_default_yfinance_fetcher` which calls
# yfinance. Returning a pandas DataFrame lets us stay vectorized without
# forcing a hard yfinance import into test modules.
PriceFetcher = Callable[[str, _dt.date, _dt.date], Any]


# Function signature for `git rev-parse HEAD`. Tests inject a stub so
# they don't depend on the current repo's actual HEAD sha.
ShaResolver = Callable[[], str]


# ---------- strategy reading ----------


def _read_strategy_for_backtest(
    vault_root: Path, slug: str
) -> tuple[str, str]:
    """Return (symbol, body_text). Raises BacktestError on any shape
    problem -- slug not found, frontmatter parse error, missing /
    malformed `order.ticker`.

    Body text is needed because `scan_backtests_for_slug` (the consumer
    side, in invest_ship_strategy.py) reads the strategy body to check
    for the `## Backtest Override` section. `run_backtest` itself does
    NOT consume body text -- but separating the read into one helper
    keeps test coverage simple.
    """
    strategy_path = vault_root / "wiki" / "strategies" / f"strategy_{slug}.md"
    if not strategy_path.exists():
        raise BacktestError(
            f"strategy_{slug}.md not found at {strategy_path}; "
            f"author the spec first or run /screen"
        )
    try:
        content = strategy_path.read_bytes()
    except OSError as exc:
        raise BacktestError(
            f"could not read {strategy_path}: {exc}"
        ) from exc
    try:
        fm = sf.parse(content)
    except ValueError as exc:
        raise BacktestError(
            f"strategy {strategy_path} frontmatter parse error: {exc}"
        ) from exc
    if not fm:
        raise BacktestError(
            f"strategy {strategy_path} has no YAML frontmatter"
        )
    order = fm.get("order")
    if not isinstance(order, dict):
        raise BacktestError(
            f"strategy {slug}: `order:` missing or not a mapping"
        )
    ticker = order.get("ticker")
    if not isinstance(ticker, str) or not ticker.strip():
        raise BacktestError(
            f"strategy {slug}: `order.ticker` missing or non-string"
        )
    return ticker.strip(), sf._split_body(content)


# ---------- simulation ----------


def _simulate(prices: Any) -> tuple[list[BacktestEntry], Any, Any]:
    """Long-only lag-1 SMA(SMA_SHORT)/SMA(SMA_LONG) crossover on Close
    prices. Returns (trades, daily_returns, equity_curve).

    Signal is computed from today's close but the position takes effect
    on the NEXT trading day (position[t] = signal[t-1]) so the
    simulation never reads tomorrow's price when making today's
    decision. This is the standard lag-1 convention; its absence would
    be a textbook look-ahead bug, and the Phase 2 sanity gate's
    thresholds assume it is present.

    Mid-price fills on close (Phase 2 MVP). Phase 4 adds slippage when
    the first paper trade reveals meaningful drag.

    Caller guarantees at least SMA_LONG + 1 bars in `prices` so both
    SMAs are defined for at least one day and at least one daily return
    is available.
    """
    import pandas as pd  # local import so test fixtures don't hard-require

    close = prices["Close"].astype(float)
    if len(close) < SMA_LONG + 1:
        raise BacktestError(
            f"insufficient price history: need at least {SMA_LONG + 1} "
            f"daily bars for SMA({SMA_LONG}) + 1-day lag, got {len(close)}"
        )
    sma_short = close.rolling(SMA_SHORT, min_periods=SMA_SHORT).mean()
    sma_long = close.rolling(SMA_LONG, min_periods=SMA_LONG).mean()
    signal = (sma_short > sma_long).astype(int)
    # Lag-1: today's position reflects yesterday's signal. `fillna(0)`
    # for the pre-warmup bars keeps the series float-free and indexable.
    position = signal.shift(1).fillna(0).astype(int)

    asset_ret = close.pct_change().fillna(0.0)
    strat_ret = position * asset_ret
    equity = (1.0 + strat_ret).cumprod()

    # Identify round-trip trades. Entry = 0->1 transition; exit = 1->0.
    # If still long at the final bar, synthesise an exit at the final
    # close so the last trade's P&L + holding days count.
    pos_shift = position.shift(1, fill_value=0).astype(int)
    entry_mask = (pos_shift == 0) & (position == 1)
    exit_mask = (pos_shift == 1) & (position == 0)

    entries_ix = list(close.index[entry_mask])
    exits_ix = list(close.index[exit_mask])
    if entries_ix and (not exits_ix or entries_ix[-1] > exits_ix[-1]):
        exits_ix.append(close.index[-1])

    trades: list[BacktestEntry] = []
    for e_ix, x_ix in zip(entries_ix, exits_ix):
        entry_price = float(close.loc[e_ix])
        exit_price = float(close.loc[x_ix])
        # Holding days = trading-day count between entry and exit. Using
        # positional distance rather than calendar-day diff keeps the
        # metric consistent with the "n_trades per 252 trading days"
        # normalisation the rest of the stats use.
        e_pos = close.index.get_loc(e_ix)
        x_pos = close.index.get_loc(x_ix)
        holding_days = int(x_pos - e_pos)
        return_pct = (exit_price / entry_price - 1.0) * 100.0
        trades.append(
            BacktestEntry(
                entry_date=_as_date(e_ix),
                exit_date=_as_date(x_ix),
                entry_price=entry_price,
                exit_price=exit_price,
                return_pct=return_pct,
                holding_days=holding_days,
            )
        )
    return trades, strat_ret, equity


def _as_date(index_value: Any) -> _dt.date:
    """Coerce a pandas DatetimeIndex entry (or datetime, or date) into a
    plain `datetime.date`. yfinance returns Timestamp objects; tests may
    pass date-anchored indexes."""
    if isinstance(index_value, _dt.datetime):
        return index_value.date()
    if isinstance(index_value, _dt.date):
        return index_value
    # pandas Timestamp has .to_pydatetime(); fall through to str parse
    # only if someone hands us something exotic (kept for safety).
    to_py = getattr(index_value, "to_pydatetime", None)
    if callable(to_py):
        return to_py().date()
    return _dt.date.fromisoformat(str(index_value)[:10])


# ---------- metrics ----------


def _compute_metrics(
    trades: list[BacktestEntry], daily_ret: Any
) -> BacktestMetrics:
    """Aggregate the raw simulation output into the §2.5 metrics shape.

    All percentages in PERCENT (not fractional). Sharpe + Sortino
    annualised with sqrt(252). Sortino uses downside deviation (negative
    daily returns only). max_dd computed from the strategy equity curve
    (cumulative-product of 1 + daily_ret).

    Guards against divide-by-zero / empty-series paths return 0.0 so a
    trivial no-trade fixture still produces a valid result rather than
    raising; those fixtures are useful in tests.
    """
    if len(daily_ret) == 0:
        return BacktestMetrics(
            sharpe=0.0,
            sortino=0.0,
            max_dd_pct=0.0,
            win_rate_pct=0.0,
            avg_winner_pct=0.0,
            avg_loser_pct=0.0,
            total_return_pct=0.0,
            n_trades=0,
            avg_trade_holding_days=0.0,
        )

    total_return_pct = float((1.0 + daily_ret).prod() - 1.0) * 100.0

    mean = float(daily_ret.mean())
    std = float(daily_ret.std(ddof=1)) if len(daily_ret) > 1 else 0.0
    sharpe = (
        (mean / std) * (TRADING_DAYS_PER_YEAR ** 0.5) if std > 0 else 0.0
    )
    downside = daily_ret[daily_ret < 0]
    d_std = (
        float(downside.std(ddof=1))
        if len(downside) > 1
        else 0.0
    )
    sortino = (
        (mean / d_std) * (TRADING_DAYS_PER_YEAR ** 0.5)
        if d_std > 0
        else 0.0
    )

    equity = (1.0 + daily_ret).cumprod()
    rolling_max = equity.cummax()
    drawdown = equity / rolling_max - 1.0
    max_dd_pct = float(drawdown.min()) * 100.0 if len(drawdown) else 0.0

    n_trades = len(trades)
    if n_trades:
        winners = [t for t in trades if t.return_pct > 0]
        losers = [t for t in trades if t.return_pct <= 0]
        win_rate_pct = len(winners) / n_trades * 100.0
        avg_winner_pct = (
            sum(t.return_pct for t in winners) / len(winners)
            if winners
            else 0.0
        )
        avg_loser_pct = (
            sum(t.return_pct for t in losers) / len(losers)
            if losers
            else 0.0
        )
        avg_hold = sum(t.holding_days for t in trades) / n_trades
    else:
        win_rate_pct = 0.0
        avg_winner_pct = 0.0
        avg_loser_pct = 0.0
        avg_hold = 0.0

    return BacktestMetrics(
        sharpe=round(sharpe, 4),
        sortino=round(sortino, 4),
        max_dd_pct=round(max_dd_pct, 4),
        win_rate_pct=round(win_rate_pct, 4),
        avg_winner_pct=round(avg_winner_pct, 4),
        avg_loser_pct=round(avg_loser_pct, 4),
        total_return_pct=round(total_return_pct, 4),
        n_trades=n_trades,
        avg_trade_holding_days=round(avg_hold, 4),
    )


# ---------- sanity gate ----------


def _apply_sanity_gate(metrics: BacktestMetrics) -> tuple[str, str]:
    """Return (look_ahead_check, reason). `reason` is empty on `passed`.

    Spec §2.5 LOCK: total_return > 500% OR max_dd > -2% OR win_rate > 85%
    trips `suspicious`. The reason string lists ALL tripped thresholds so
    Keith sees every dimension at once when writing the override note.
    """
    tripped: list[str] = []
    if metrics.total_return_pct > TOTAL_RETURN_PCT_THRESHOLD:
        tripped.append(
            f"total_return={metrics.total_return_pct:.1f}% > "
            f"{TOTAL_RETURN_PCT_THRESHOLD:.0f}%"
        )
    if metrics.max_dd_pct > MAX_DD_PCT_THRESHOLD:
        tripped.append(
            f"max_dd={metrics.max_dd_pct:.1f}% > "
            f"{MAX_DD_PCT_THRESHOLD:.0f}%"
        )
    if metrics.win_rate_pct > WIN_RATE_PCT_THRESHOLD:
        tripped.append(
            f"win_rate={metrics.win_rate_pct:.1f}% > "
            f"{WIN_RATE_PCT_THRESHOLD:.0f}%"
        )
    if tripped:
        return "suspicious", "; ".join(tripped)
    return "passed", ""


# ---------- capture writer ----------


def _ensure_backtests_index_stub(backtests_dir: Path) -> None:
    """Create `raw/backtests/index.md` if missing. K2Bi convention: every
    wiki/ + raw/ + review/ subfolder has an `index.md` with frontmatter +
    `up:` pointer. The stub is enough to satisfy invest-lint; Keith or
    a future /lint deep run fleshes out the body."""
    index_path = backtests_dir / "index.md"
    if index_path.exists():
        return
    stub = (
        "---\n"
        "tags: [index, backtests, raw]\n"
        f"date: {_dt.date.today().isoformat()}\n"
        "type: index\n"
        "origin: k2bi-generate\n"
        'up: "[[raw/index]]"\n'
        "---\n"
        "\n"
        "# raw/backtests\n"
        "\n"
        "Immutable per-run backtest captures written by `invest-backtest`"
        " (m2.15).\n"
        "\n"
        "Filename shape: `YYYY-MM-DD_<slug>_backtest.md` "
        "(same-day re-runs insert `_HHMMSS` between the date and slug).\n"
        "\n"
        "Each capture is read by `/invest-ship --approve-strategy`'s "
        "backtest-gate scan (spec §3.5): `look_ahead_check: passed` "
        "proceeds; `suspicious` requires a `## Backtest Override` "
        "section in the strategy body.\n"
    )
    sf.atomic_write_bytes(index_path, stub.encode("utf-8"))


def _resolve_capture_filename(
    backtests_dir: Path, slug: str, now: _dt.datetime
) -> Path:
    """Compute a unique capture filename.

    First-of-day: `<date>_<slug>_backtest.md` (bare form).
    Collision: `<date>_<HHMMSS>_<slug>_backtest.md` (HHMMSS suffix).
    Same-second collision: append `_<microseconds>` until unique
    (prevents Codex R9 #2 HIGH -- two reruns in the same second
    previously collided and the atomic-write overwrote the earlier
    capture, breaking the immutable per-run audit trail).

    Note: the gate scanner (`scan_backtests_for_slug`) only trusts
    writer-produced filenames matching `_is_writer_produced_filename`,
    which accepts only bare + HHMMSS forms. Microsecond-appended
    filenames fall OUTSIDE that contract, so they produce captures
    for Keith's audit trail but do NOT participate in approval
    selection until he manually renames them. That is intentional:
    same-second reruns are almost always an automation glitch, not a
    legitimate evaluation path, and surfacing them separately forces
    operator attention.
    """
    date_prefix = now.strftime("%Y-%m-%d")
    bare_name = f"{date_prefix}_{slug}_backtest.md"
    bare_path = backtests_dir / bare_name
    if not bare_path.exists():
        return bare_path
    hhmmss = now.strftime("%H%M%S")
    hhmmss_path = backtests_dir / (
        f"{date_prefix}_{hhmmss}_{slug}_backtest.md"
    )
    if not hhmmss_path.exists():
        return hhmmss_path
    # Same-second collision. Append microseconds; loop until unique
    # in the extreme case two runs use the same `now` arg. Microsecond
    # has 6 digits, so a first-pass collision is astronomically
    # unlikely in practice, but the loop is defensive against the
    # exact `now=fixed_datetime` pattern tests may exercise.
    us = now.microsecond
    for i in range(1000):
        candidate = backtests_dir / (
            f"{date_prefix}_{hhmmss}_{us + i}_{slug}_backtest.md"
        )
        if not candidate.exists():
            return candidate
    raise BacktestError(
        f"could not allocate unique capture filename after 1000 "
        f"attempts in {backtests_dir}; check for filesystem issues"
    )


def _render_capture(
    slug: str,
    symbol: str,
    reference_symbol: Optional[str],
    window: BacktestWindow,
    metrics: BacktestMetrics,
    look_ahead_check: str,
    look_ahead_check_reason: str,
    last_run: _dt.datetime,
    strategy_commit_sha: str,
    source_version: str,
) -> bytes:
    """Render the full capture file content: frontmatter + body.

    Frontmatter schema matches spec §2.5 EXACTLY. Body follows the §2.5
    example -- `[!robot]` callout + Strategy Reference + Sanity Gate
    Result + Metrics table + Limitations. Trade Distribution is elided
    for Phase 2 MVP (kept out of the body to stay under 2kB per capture;
    trades remain in the in-memory BacktestResult for callers that want
    them).
    """
    fm: dict[str, Any] = {
        "tags": ["backtest", slug, "raw"],
        "date": last_run.date().isoformat(),
        "type": "backtest",
        "origin": "k2bi-generate",
        "up": "[[backtests/index]]",
        "strategy_slug": slug,
        "strategy_commit_sha": strategy_commit_sha,
        "backtest": {
            "window": {
                "start": window.start.isoformat(),
                "end": window.end.isoformat(),
            },
            "source": "yfinance",
            "source_version": source_version,
            "symbol": symbol,
            "reference_symbol": (
                reference_symbol
                if reference_symbol is not None
                else symbol
            ),
            "metrics": {
                "sharpe": float(metrics.sharpe),
                "sortino": float(metrics.sortino),
                "max_dd_pct": float(metrics.max_dd_pct),
                "win_rate_pct": float(metrics.win_rate_pct),
                "avg_winner_pct": float(metrics.avg_winner_pct),
                "avg_loser_pct": float(metrics.avg_loser_pct),
                "total_return_pct": float(metrics.total_return_pct),
                "n_trades": int(metrics.n_trades),
                "avg_trade_holding_days": float(metrics.avg_trade_holding_days),
            },
            "look_ahead_check": look_ahead_check,
            "look_ahead_check_reason": look_ahead_check_reason,
            "last_run": last_run.isoformat(),
        },
    }
    fm_text = yaml.safe_dump(
        fm, sort_keys=False, allow_unicode=True, default_flow_style=False
    )
    # Body. The Limitations section is a hard-coded locked list per
    # spec §2.5 -- Phase 2 MVP yfinance + mid-price + single-strategy
    # caveat surface is fixed, so the language stays consistent across
    # runs and is greppable for Keith + downstream readers.
    body_lines: list[str] = [
        "> [!robot] K2Bi analysis -- yfinance sanity-check backtest",
        "",
        "## Strategy Reference",
        "",
        f"- Slug: `{slug}`",
        f"- Commit SHA at backtest time: `{strategy_commit_sha}`",
        f"- Strategy file: [[strategy_{slug}]]",
        "",
        "## Sanity Gate Result",
        "",
    ]
    if look_ahead_check == "passed":
        body_lines.append(
            "**Result:** passed (no look-ahead-bias thresholds tripped)."
        )
    else:
        body_lines.append(f"**Result:** suspicious -- {look_ahead_check_reason}")
        body_lines.append("")
        body_lines.append(
            "Approval of any strategy referencing this backtest requires "
            "a `## Backtest Override` section in the strategy body "
            "explaining why these thresholds are acceptable (spec §3.5)."
        )
    body_lines += [
        "",
        "## Metrics",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Sharpe (annualised) | {metrics.sharpe:.2f} |",
        f"| Sortino (annualised) | {metrics.sortino:.2f} |",
        f"| Max drawdown | {metrics.max_dd_pct:.2f}% |",
        f"| Win rate | {metrics.win_rate_pct:.2f}% |",
        f"| Avg winner | {metrics.avg_winner_pct:.2f}% |",
        f"| Avg loser | {metrics.avg_loser_pct:.2f}% |",
        f"| Total return | {metrics.total_return_pct:.2f}% |",
        f"| Trades | {metrics.n_trades} |",
        f"| Avg trade holding (days) | {metrics.avg_trade_holding_days:.1f} |",
        "",
        "## Limitations",
        "",
        "- yfinance returns what it has today (no point-in-time data store)",
        "- mid-price fills (no slippage modeling)",
        "- single strategy in isolation (no portfolio context)",
        "- lag-1 SMA(20)/SMA(50) crossover baseline, NOT the strategy's "
        "actual entry/exit rules (Phase 2 MVP; Phase 4 replaces with "
        "rule extraction when walk-forward harness ships)",
        "",
    ]
    body = "\n".join(body_lines)
    return f"---\n{fm_text}---\n\n{body}".encode("utf-8")


# ---------- price fetch ----------


def _default_yfinance_fetcher(
    symbol: str, start: _dt.date, end: _dt.date
) -> Any:
    """Production PriceFetcher: pull daily bars from yfinance.

    yfinance's `end` is EXCLUSIVE -- add one calendar day so the window
    is inclusive on both ends (tests that stub this fetcher use the
    same inclusive convention, so production + test paths agree).

    Raises BacktestError on empty-data return so callers get a typed
    failure rather than a mysterious DataFrame-shape error downstream.
    """
    import yfinance as yf

    end_inclusive = end + _dt.timedelta(days=1)
    df = yf.download(
        symbol,
        start=start.isoformat(),
        end=end_inclusive.isoformat(),
        interval="1d",
        progress=False,
        auto_adjust=True,
        multi_level_index=False,
    )
    if df is None or len(df) == 0:
        raise BacktestError(
            f"yfinance returned no data for {symbol} "
            f"({start.isoformat()} to {end.isoformat()}); "
            f"check symbol spelling + date range"
        )
    # MiniMax R1 #2 HIGH: yfinance has a history of breaking DataFrame
    # schema changes across minor releases (column names, multi-level
    # indexing, auto_adjust semantics). Requirements pins an upper
    # bound but a user with a dirty venv or CI cache could still hit a
    # newer release. Asserting the `Close` column is present here fails
    # loudly with an actionable message rather than letting `_simulate`
    # crash on a KeyError that gets wrapped into a generic BacktestError.
    if "Close" not in df.columns:
        raise BacktestError(
            f"yfinance schema mismatch for {symbol}: expected `Close` "
            f"column, got columns {list(df.columns)}. Likely yfinance "
            f"version drift; pin requirements.txt upper bound or "
            f"update the fetcher."
        )
    return df


def _default_sha_resolver() -> str:
    """Production ShaResolver: read K2Bi repo HEAD commit sha.

    `git rev-parse HEAD` (full 40-char sha) per spec §2.5
    `strategy_commit_sha` field. Falls back to `"unknown"` if git fails
    so a backtest never fails just because the working tree is in an
    unusual state (e.g. CI without git).
    """
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return out.strip() or "unknown"
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return "unknown"


def _yfinance_version() -> str:
    """Return the installed yfinance version string, or `"unknown"` if
    yfinance is unavailable at runtime. Recorded in capture frontmatter
    per spec §2.5 `source_version`."""
    try:
        import yfinance as yf

        return getattr(yf, "__version__", "unknown")
    except ImportError:
        return "unknown"


# ---------- main entry ----------


def run_backtest(
    slug: str,
    *,
    vault_root: Path,
    window_start: Optional[_dt.date] = None,
    window_end: Optional[_dt.date] = None,
    reference_symbol: str = "SPY",
    now: Optional[_dt.datetime] = None,
    price_fetcher: Optional[PriceFetcher] = None,
    sha_resolver: Optional[ShaResolver] = None,
    source_version: Optional[str] = None,
) -> BacktestResult:
    """Execute a yfinance sanity-check backtest for strategy `<slug>`.

    Reads `<vault_root>/wiki/strategies/strategy_<slug>.md`, extracts the
    primary ticker from `order.ticker`, pulls 2y of daily bars, runs a
    lag-1 SMA crossover baseline on the symbol, computes §2.5 metrics,
    applies the sanity gate, and atomic-writes a per-run capture under
    `<vault_root>/raw/backtests/`.

    Strategy file is NEVER written. Check D content-immutability holds.

    Args:
        slug: strategy file stem minus the `strategy_` prefix (matches
            `sf.derive_retire_slug` contract).
        vault_root: K2Bi vault root (contains `wiki/strategies/` +
            `raw/backtests/`).
        window_start / window_end: optional overrides. Default is the
            last DEFAULT_WINDOW_DAYS (730) calendar days from `now.date()`.
        reference_symbol: benchmark ticker. Silently dropped (reference
            falls back to the strategy symbol itself, captured as
            `reference_symbol: <symbol>`) when the strategy IS the
            reference -- we never benchmark SPY against SPY.
        now: stamp time for `last_run` frontmatter + capture filename.
            Defaults to `datetime.now(timezone.utc)`. Tests pin this for
            determinism.
        price_fetcher / sha_resolver: dependency-injection seams for
            tests. Production callers leave them None; `run_backtest`
            uses the default yfinance + git implementations.
        source_version: override for the recorded `source_version`
            field. Production leaves this None; tests pin it so capture
            bytes are deterministic across yfinance upgrades.

    Returns:
        BacktestResult with `path`, metrics, trades, gate verdict, and
        the key provenance fields needed for commit / log writes.

    Raises:
        BacktestError: any strategy-read / price-fetch / schema failure.
    """
    if now is None:
        now = _dt.datetime.now(_dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_dt.timezone.utc)
    today = now.date()

    symbol, _strategy_body = _read_strategy_for_backtest(vault_root, slug)

    if window_end is None:
        window_end = today
    if window_start is None:
        window_start = today - _dt.timedelta(days=DEFAULT_WINDOW_DAYS)
    if window_start >= window_end:
        raise BacktestError(
            f"invalid backtest window: start ({window_start}) must be "
            f"before end ({window_end})"
        )
    window = BacktestWindow(start=window_start, end=window_end)

    # Phase 2 MVP: if the strategy IS the reference symbol, record the
    # same ticker in `reference_symbol` (spec §2.5 allows; §2.5 example
    # shows SPY vs SPY as the degenerate case). Downstream readers treat
    # `reference_symbol == symbol` as "no benchmark this run".
    ref = reference_symbol if reference_symbol != symbol else None

    fetcher = price_fetcher or _default_yfinance_fetcher
    try:
        prices = fetcher(symbol, window_start, window_end)
    except BacktestError:
        raise
    except Exception as exc:  # noqa: BLE001
        # Wrap third-party errors so the SKILL.md body gets a typed
        # message. yfinance can raise a grab-bag of JSONDecodeError,
        # KeyError, RequestException; we surface them all as
        # BacktestError so the exit-1 path is uniform.
        raise BacktestError(
            f"price fetch for {symbol} failed: {exc}"
        ) from exc

    trades, daily_ret, _equity = _simulate(prices)
    metrics = _compute_metrics(trades, daily_ret)
    look_ahead_check, look_ahead_reason = _apply_sanity_gate(metrics)

    sha_fn = sha_resolver or _default_sha_resolver
    strategy_commit_sha = sha_fn()
    if source_version is None:
        source_version = _yfinance_version()

    backtests_dir = vault_root / "raw" / "backtests"
    backtests_dir.mkdir(parents=True, exist_ok=True)
    _ensure_backtests_index_stub(backtests_dir)

    capture_path = _resolve_capture_filename(backtests_dir, slug, now)
    content = _render_capture(
        slug=slug,
        symbol=symbol,
        reference_symbol=ref,
        window=window,
        metrics=metrics,
        look_ahead_check=look_ahead_check,
        look_ahead_check_reason=look_ahead_reason,
        last_run=now,
        strategy_commit_sha=strategy_commit_sha,
        source_version=source_version,
    )
    sf.atomic_write_bytes(capture_path, content)

    return BacktestResult(
        path=capture_path,
        slug=slug,
        symbol=symbol,
        reference_symbol=ref,
        window=window,
        metrics=metrics,
        look_ahead_check=look_ahead_check,
        look_ahead_check_reason=look_ahead_reason,
        last_run=now,
        strategy_commit_sha=strategy_commit_sha,
        source_version=source_version,
        trades=trades,
    )
