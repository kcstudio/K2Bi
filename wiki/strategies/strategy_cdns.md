---
tags:
- strategy
- cdns
- k2bi
date: '2026-06-07'
type: strategy
origin: k2bi-generate
up: '[[index]]'
name: cdns
slug: cdns
strategy_type: hand_crafted
risk_envelope_pct: 0.0025
regime_filter: []
ticker: CDNS
status: proposed
sigid: 2026-06-07-cdns-eda-compute-supply
thesis_ref: '[[../tickers/CDNS]]'
order:
  ticker: CDNS
  side: buy
  qty: 1
  order_type: LMT
  limit_price: 330.0
  stop_loss: 295.0
  time_in_force: DAY
forward_guidance_check:
  completed_at: '2026-06-10T23:20:10.875865'
  status: pass
  override_reason: null
  waive_reason: null
  thresholded_metrics:
  - metric: none
    locked_threshold_text: No single thresholded guide metric
    guide_source_text: 'operator-pasted: thesis is valuation/qualitative; no single
      locked guide metric reconciles'
    guide_range_text: no quantitative guide
    sits_inside_guide: false
---

# Strategy: cdns

## How This Works

CDNS is a wide-moat EDA duopoly priced for perfection (~$376 = forward P/E ~47x, no margin of safety), so this is a patient, valuation-disciplined limit-entry plan that does nothing until price comes to it. It only engages on a de-rating toward the ~$330 fair-value floor, where the moat is no longer fully priced and risk/reward turns favorable. Buy only via a $330 limit order with the thesis conditions intact (firm backlog/RPO, normal China access, recurring mix holding >=80%), scale out into the $370/$410/$450 fair-value range, hard-stop at $295 below the floor, and kill on any thesis-invalidation signal. Direction is neutral at the current price; the plan stays dormant unless a de-rating entry triggers.

## Bucket Rules

- Bucket 4 (thesis-driven swing): exit the full position when thesis-breaking news lands -- a persistent China export restriction, a durable recurring-mix break below 80%, or a Synopsys-Ansys-driven competitive step-change.

## Entry Rules

- Enter ONLY via a $330.00 limit order on a de-rating toward the fair-value floor; do not chase at the current ~$376 price.
- Confirm backlog/RPO stay firm at the next earnings before entering.
- Confirm China access remains operationally normal (no new persistent BIS/export restrictions) before entering.
- Do NOT enter if recurring mix falls further below 80%, backlog growth stalls, or China export restrictions are re-imposed persistently.

## Stop Rules

- Initial hard stop at $295.00 (-11% from the $330 entry), below the fair-value floor, to cap risk on a de-rating overshoot or a China/operational shock.
- Trail the stop beneath each target band as backlog/RPO and recurring-mix data confirm durability.

## Target Rules

- T1 $370.00 (+12%): sell 33% -- re-rate toward fair-value mid after a de-rating entry.
- T2 $410.00 (+24%): sell 33% -- top of the analyst fair-value range on sustained backlog/margins.
- T3 $450.00 (+36%): sell the remaining 34% -- bull extension if AI bookings re-accelerate durable growth.

## Hold Rules

- Maximum hold is 12 months.
- Reassess at Q2 FY2026 earnings (estimated 2026-07-27), on any new BIS/China export action, or on a backlog/RPO trend break.

## Kill Rules

- Kill on any thesis-invalidation signal: recurring mix breaking below 80% for two consecutive quarters, persistent China export restrictions, or two consecutive quarters of decelerating Core EDA growth.
- Exit regardless of price if the forward multiple re-expands without estimate revisions.

## Forward Guidance Reconciliation

- Status: pass
- none: No single thresholded guide metric; guide=no quantitative guide; inside=False

## Accepted Gaps

- No regime_filter for this first proposed spec; entry discipline is carried by the $330 limit plus the thesis-condition gates.
- Position sizing is validator-owned (execution/validators/config.yaml position_size cap), not encoded in this spec.

## Accepted Gaps for Phase 3.8b First Paper Trade

The following plan-review architecture concerns are explicitly accepted as
known gaps for Phase 3.8b (first paper trade per ticker). Each is captured
here so that plan-review at /ship time does not re-surface them as novel
findings; future-trade iterations close them per the roadmap below.

### Gap 1 -- Kill-criterion override keyed to guide endpoints

Kill criteria are deliberately keyed to management's published guide
endpoints because the thesis IS that management hits guide; a mechanical
trigger when guide breaks is the intended downside discipline.
Future iteration: explore a 50%-of-guide variant for drawdown tolerance.
See L-2026-04-27-005.

### Gap 2 -- MKT-gap-risk on small fractional sizing

At 0.25% NAV-at-risk fractional sizing, a worst-case 20% gap-down at the
open puts ~0.05% NAV over budget; bounded and acceptable for first paper
trade. Future iteration: opening-range-confirmation order type once the
validator supports it.

### Gap 3 -- Conviction-linked sizing absent

Sizing is locked at the architect-decided fractional cap for the first
paper trade per ticker; not conviction-driven. Future iteration:
implement a `bear_conviction` -> NAV-at-risk formula from trade #2
onwards.

### Gap 4 -- Empty regime_filter

Phase 4 immediate narrative-reversal kill criterion (b) provides
regime-related exit discipline. `regime_filter:` for entry-time
discipline lands in a future iteration. Default is empty until
ticker-specific regime parameters are identified at T10 by the operator.
