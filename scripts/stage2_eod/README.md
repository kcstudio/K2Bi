# Local end-of-day practice

Run from the repository root. Uses only saved bytes, no broker connection or source fetch. The historical saved IBKR daily bars are display-only, so this replay creates no virtual fills.

```sh
python3 -m scripts.stage2_eod.cli run \
  --research proposals/dashboard-ux-2026-09-26/first-cycle-receipt.json \
  --prices proposals/stage2-ibkr-price-check-2026-09-26/ibkr-five-daily.json \
  --as-of 2026-09-26T04:00:00Z \
  --state-dir /tmp/k2bi-eod-practice-state \
  --output /tmp/k2bi-eod-practice.html
open /tmp/k2bi-eod-practice.html
python3 -m scripts.stage2_eod.cli rebuild \
  --state-dir /tmp/k2bi-eod-practice-state \
  --output /tmp/k2bi-eod-practice.html
```

The rebuilt page is a standalone offline HTML file with the accepted six tabs, trade-guide link, glossary and teaching-example toggle. It displays evaluation, source and session dates. The virtual books are separate from the unavailable IBKR paper-account snapshot.

`store.recover(Path(...))` commits the exact durable unfinished request after a crash without replacement inputs. `latest` and `rebuild` verify all persisted history without rewriting receipt JSON. Same-cycle identical input returns the same receipt. Changed inputs conflict. Corruption fails closed; preserve the state directory for diagnosis rather than resetting it.

Only explicitly disclosed offline fixtures supplied to the low-level ledger/store APIs are eligible for virtual fills. The CLI accepts the sealed research receipt and original IBKR batch schema. It never promotes saved provider bars into eligible prices. Tests use invented inputs and a real XNYS calendar, including holidays and daylight saving. The simulator freezes whole-share quantity before the first later session open, uses that session's valid close, rejects excessive gap costs, and never catches up missed instructions. Each book starts at $10,000, with cash-only research entries capped at 20% entry cost and 1% stop risk, maximum two positions, 3% close stop and 5% target. Fills assume five basis points adverse slippage and $0.50 per transaction. Intraday always waits on daily bars.

No scheduling, deployment, source refresh or order controls are included.
