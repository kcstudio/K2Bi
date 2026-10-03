# Stage 3 paper snapshot (read only)

Renders the accepted stage 2 EOD desk with paper account holdings, balances,
historical tracked-company closing prices and recorded paper currency cash.

## CLI

    python -m scripts.stage3_paper.cli render \
        --state-dir <state> \
        --as-of <AS_OF> \
        --snapshot <paper_snapshot.json> \
        --prices <ibkr_daily_batch.json> \
        --cash-snapshot <ibkr_paper_currency_cash.json> \
        --output <page.html>

Use a real ISO-8601 timestamp with an explicit UTC offset for `--as-of`; never
a fixed midnight placeholder. `--snapshot`, `--prices` and `--cash-snapshot`
are all optional. Unreadable, non-regular or colliding source paths fail closed
with exit 2 and no output.

## Time

Nothing is captured at render time. `--as-of` is only used to evaluate the
freshness of sources you supply; snapshots staler than the freshness window and
cash staler than 15 minutes are marked stale, and future timestamps fail closed.

## Manual capture and the read-only helper

The snapshot capture is manual. `cash_capture.py` is a read-only helper that
uses the client id auto-leased by `scripts/gateway-query.sh` in the 90..99 range, never 1, and connects to the
paper Gateway on 127.0.0.1:4002, requires the DUQ220152 account, and disconnects
in an outer `finally` even when the connection fails. It issues no order,
cancel, market-data or service API calls and prints JSON only.

## Currency

Currency cash is kept in its native currency, negatives preserved and never
summed or converted. `BASE` is never USD. A missing USD balance is reported as
unknown USD. `AccountReady` false or unknown means cash reliability is unproven
and the balances are not shown as spendable or order-ready.

## Scope

Prices are historical closing prices only, not current quotes, and imply no
trade eligibility or order readiness. No orders, activations or deployments
are performed. Successful returned bars prove historical access only. The recorded batch shape does
not establish adjustment treatment or live-feed entitlement. Completed-session lag
is evaluated with the XNYS calendar; daily bars are not intraday prices.

Manual cash capture:

    scripts/gateway-query.sh -f scripts/stage3_paper/cash_capture.py > <cash_snapshot.json>
