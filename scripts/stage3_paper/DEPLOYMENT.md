# Isolated read-only desk: deployment preparation

This package is prepared locally. It has not been deployed and does not activate the trading engine. The desk displays saved evidence and an offline action card. No broker submission, cancellation, currency conversion, automatic refresh or service management is included.

## Payload boundary

Build an isolated directory from the exact reviewed Git revision, separate from any engine checkout. Copy only the runtime files below. Never rsync the existing dirty working tree. Do not copy execution code, service definitions, hooks, Git metadata, validators, kill sentinels, private broker captures, proposals, or unreviewed quote adapters. Test fixtures are public and remain test-only.

The command-line renderer reads execution/validators/config.yaml as a context input. That file is not part of this payload. A future operator must provision an independently approved read-only configuration at that expected relative location, compare its fingerprint, and verify access permissions. Do not copy or change engine configuration to satisfy the desk. The Python API scripts.stage3_paper.desk.render accepts config_raw bytes or None; None leaves context unknown. This is distinct from the CLI, which has no configuration override and fails closed unless the fixed read-only file is available. Displayed limits are not evaluated risk checks.

Saved account, cash, price and request-proof files are separate private operator inputs. Preserve their exact bytes, capture times, account identity and hashes. Do not bundle them into Git or public artifacts. The existing dated account and cash captures are manual snapshots, not a live balance display. Historical closes are reference data only, never executable quotes or proof of live-feed entitlement.

## Verification before any future deployment

Use a clean exported reviewed revision with the recorded Python and library versions below. Verify every runtime hash. Run the focused Stage 2 and Stage 3 tests on that export. Render only in a fresh allowed output directory and verify the source bytes remain unchanged. Repeated renders at the same explicit evaluation time must be identical. Test unavailable, stale and mismatched-proof inputs. Keep old and new holdings/cash capture dates separate. Unknown cash, settlement, pending orders, quote, recommendation, approved mandate and risk status must remain unknown or not run.

Prepare a versioned immutable payload directory and a checksum inventory. Keep the preceding reviewed version and its inventory for rollback. A future approved deployment should switch a separate desk directory atomically, verify its checksums and offline render, and restore the preceding directory if verification fails. No engine/service restart or shared-checkout overwrite belongs to this procedure. This document does not authorize deployment.

## Remaining Stage 3 gates

The local read-only presentation and offline preview are useful preparation. Stage 3 trading activation remains incomplete. Resolve account currency exposure and independently verify settled USD cash and pending orders. A negative USD cash balance is a currency debit, not profit or loss; HKD does not automatically fund a USD order. No autonomous currency conversion is proposed.

Obtain dated current research and an approved paper mandate before forming a real candidate. Establish usable current execution-price evidence, then run the existing deterministic validators with complete order and portfolio risk context. Verify journal/broker reconciliation, recovery behavior, sizing, circuit breakers, kill control, account limits and operator intervention procedure in their proper engine boundaries. This desk does not duplicate or bypass them.

Only after those evidence and mandate gates are complete can a concrete deployment/activation packet specify host, isolated payload, approved configuration, refresh cadence, monitoring, rollback and account scope for human authorization. Services, engine activation and paper orders remain outside this local campaign. Existing planning estimate is approximately 5 to 10 focused development days for the remaining full Stage 3 sequence; it is not a promised activation date.

## Prepared runtime versions and hashes

These hashes are a preparation inventory. Final delivery evidence must bind them to the reviewed code revision; do not treat an unreviewed local candidate as shipped.

Python 3.12.2; PyYAML 6.0.2, exchange_calendars 4.13.2, pandas 3.0.1, numpy 2.2.6.

| Runtime path | SHA-256 |
|---|---|
| `scripts/__init__.py` | `c6892456289047178a2d003358e3bacf821c454a87142fa829f5fb0f165832b5` |
| `scripts/stage2_eod/__init__.py` | `b6dcd9c844438b19d10144c3dbc979d3818c1f2a2d0bbc6f694c2ba607ea914d` |
| `scripts/stage2_eod/cli.py` | `d4b2481ca7fb4a656f06fcdbd10a43f7457de656d71e2312cef59c73c52ef46d` |
| `scripts/stage2_eod/desk.py` | `e59613ea54a2c0ec473e5832858fa6b6b7bd8575f1bdc2c0dfbb0a1f0dfe9c2f` |
| `scripts/stage2_eod/inputs.py` | `3d96733bb07df596710773d0462aaafaff8291c95e8fdd832079faf1a2d83d12` |
| `scripts/stage2_eod/ledger.py` | `e1fc02ae656004755d88b0a3c62274ad03f8745525e03f30120d993a47a79588` |
| `scripts/stage2_eod/store.py` | `0b6534d8cd3485de900f57726efda55cd180ec2dad2185ad3c3614863661a99e` |
| `scripts/stage2_eod/template.html` | `b7a4c9ea68827daa61ba6874e22c46bebf4c3aae947015d21936b5c79a112b08` |
| `scripts/stage2_quotes/__init__.py` | `62414e2201118ceec3754cfd645c09179f60689f22cc99163c81702848e1fbf2` |
| `scripts/stage2_quotes/contract.py` | `bfc8c356d4640c966cf498935b74626316e8062fb861ffbdd635955ca1e97f33` |
| `scripts/stage2_quotes/ibkr_adapter.py` | `ab3a2f90e5c503097b8b8d938d5a1309b98620b83c86a7d9ed0088d27415205d` |
| `scripts/stage2_quotes/ibkr_batch.py` | `08847f981d455e54a4575f6e6c45ea73f4014efc2a90ed909a1240799bd1ae86` |
| `scripts/stage3_paper/__init__.py` | `6ec3c31afd022cce589cba0a281edef5b105a0c2be30661a42612e918f545d29` |
| `scripts/stage3_paper/action_card.py` | `a73bc8c7e93e487b8adfe6e95815369ed22ce5c110b42c67a7e86294e277fe13` |
| `scripts/stage3_paper/capture.py` | `bf9e16a7cbb3b8d4f0b7ed13d945d243b77467dbb1092533eeec254a96734886` |
| `scripts/stage3_paper/cash_capture.py` | `f0cd29b63365ddf6fd9877c97b9e9461d517e3e625e66ecebfb587ecbc299d89` |
| `scripts/stage3_paper/cli.py` | `e423ee7f0b1448d24579f97fc26b25e04da988058de9670a0ed1e6d8d0649d74` |
| `scripts/stage3_paper/desk.py` | `2ccfc52eb442db60f190dee53d23e2cb5bcd9e142504338f6c5c3c0bf07df419` |
| `scripts/stage3_paper/readiness.py` | `8a82e65f47f652bf2668312f8f0f51e26a4990de9368f15a7cbd45c5516f946f` |
| `scripts/stage3_paper/reference.py` | `38937a5d5b6a71234894046b5faa59fad5467a3293951c3c8d71f5dff434ec25` |
| `scripts/stage3_paper/snapshot.py` | `ac44b117169c19e29c5fe59325cf4531234657d74daba01270e5ab60f74885c9` |
