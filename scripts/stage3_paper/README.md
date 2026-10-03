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

## Offline action card and provenance

The paper tab shows NO PROPOSAL: WAIT with candidate, quantity, cost and risk
withheld until current research, funding and risk context exist. Negative USD
cash is a debit balance, not profit or loss. HKD is not automatically USD.
Settled cash and pending orders remain unknown; actual validators are not run.
Risk config is read only and fingerprinted as context, never approval.

Optional --price-proof supplies a hash-bound acquisition sidecar matched to
the reviewed finite capture program and exact request settings. This verifies
saved historical reference provenance, not live entitlement or fill permission.
The risk context uses the unchanged execution/validators/config.yaml. No engine
code or order API is invoked. All refreshes are manual. Invented examples are
explicitly labelled and have no action controls or execution permission.


Account readiness is a separate saved shape, supplied with `--account-readiness` and optional `--account-proof`. It records separately timed account and API-visible open-order request completions through the finite read-only Gateway helper. Its standalone capture uses only stdlib and installed ib_async, with leased client ID 90 through 99, StartupFetch(0), no binding, no market data, and cleanup on every path.

A completed API-visible snapshot can show no visible API orders at capture. It does not prove all manual/user orders are absent or grant permission. Missing completion, attribution, reliable account flag, provenance or freshness stays unknown. Order quantities and limits are observations, not executable quotes; remaining quantities and reserved commitments are unverified. Settlement totals retain reported currency and scope. Native settled USD, trading permissions and restrictions remain unknown. Positive cash and AccountReady do not approve trading. The actual card continues to WAIT with proposal fields withheld.

Keep raw snapshots and acquisition proofs private and immutable. A proof binds exact source, request settings, raw bytes and capture time for ordinary saved integrity. It is not malicious-forgery authentication. Refresh is manual, and evaluation time must be explicit. This package does not submit, cancel or bind orders, change permissions, convert currencies, activate services or bypass risk validators.

## Supplied dated research brief

`--research-evidence PATH` accepts strict `offline-research-evidence` JSON. It displays a supplied brief, reported idea and separate research/save/expiry/evaluation dates. Recorded means supplied, not authenticated research; hashes and fresh dates do not verify source contents or approve a recommendation. References are passive escaped text and are never opened or fetched. Fixtures are explicitly invented. Missing, stale or supplied briefs keep the actual card WAIT with candidate, quantity, cost and risk withheld. Current executable quote, approved mandate, funding, restrictions, risk and recovery remain separate missing checks. Old company replay and account snapshots retain their own dates. No broker or external source requests occur.


### Saved source and price evidence

The manual renderer accepts `--research-proof`, `--source-g`, and `--source-cdns` with a dated `--research-evidence` brief. It checks the actual supplied SEC response bytes, their company identity and receipt hashes against the reviewed acquisition code. Matching bytes is not independent approval of the research conclusion. References remain passive.

`--quote-snapshot` and optional `--quote-proof` display a finite saved paper Gateway capture. Requested delayed fallback and observed feed are separate. The broker clock, native last-trade timestamp, local receipt time and render evaluation remain distinct. Missing callbacks or timestamps stay unknown; old data stays stale. Historical closes and saved snapshots are not executable prices. Every actual action card remains WAIT with quantity, cost and risk withheld.

The standalone `quote_capture.py` is an explicit operator read through the existing Gateway helper only after source review. It requests three fixed instruments, no paid regulatory snapshots, and cleans up only unfinished market-data request IDs. Running the renderer never refreshes sources or the broker. Deployment and paper activation are separate gates.

Offline order preparation uses `--risk-context` and `--recovery-context` supplied JSON files. Missing, stale, recorded or incomplete evidence stays WAIT with amounts withheld. Only explicitly invented complete contexts exercise the unchanged production validators and pure journal/broker reconciliation. Later validators are not run after the first rejection. Diagnostics are never applied, and no engine, broker submission or journal writer is imported. Native USD funding, stops, marks and complete pending commitments cannot default to zero. This local preparation is not paper-trading approval or deployment.
