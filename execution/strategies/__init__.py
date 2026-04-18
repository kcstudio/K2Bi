# cash-only invariant: Bundle 2 architect requires every new file in
# execution/strategies/ to EITHER import
# execution.risk.cash_only.check_sell_covered OR carry an explicit
# "no sell-side paths" header. This package init declares nothing
# order-related; enforcement is per-module below.
"""Strategy parsing + evaluation.

Two responsibilities, one per submodule:

    loader.py  -- parse wiki/strategies/<name>.md into typed
                  dataclasses. Detects mtime drift AFTER approval and
                  surfaces it so /invest-ship can re-approve; never
                  reloads a mutated file into engine state.

    runner.py  -- pure evaluation of an ApprovedStrategySnapshot
                  against a MarketSnapshot + engine context. Emits a
                  CandidateOrder or None. No I/O, no connectors, no
                  validator calls -- the engine owns those.

Types (StrategyDocument, ApprovedStrategySnapshot, CandidateOrder,
MarketSnapshot) live in strategies.types so Bundle 3
(invest-propose-limits) and Bundle 4 (invest-bear-case + backtest)
import from one place.
"""
