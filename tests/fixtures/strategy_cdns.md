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
status: approved
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
approved_at: '2026-06-11T03:14:06.199024+00:00'
approved_commit_sha: 0f7ff66
---

# Strategy: cdns

## How This Works

CDNS is a wide-moat EDA duopoly priced for perfection (~$376 = forward P/E ~47x, no margin of safety), so this is a patient, valuation-disciplined limit-entry plan that does nothing until price comes to it. It only engages on a de-rating toward the ~$330 fair-value floor, where the moat is no longer fully priced and risk/reward turns favorable. Buy only via a $330 limit order with the thesis conditions intact (firm backlog/RPO, normal China access, recurring mix holding >=80%), scale out into the $370/$410/$450 fair-value range, hard-stop at $295 below the floor, and kill on any thesis-invalidation signal. Direction is neutral at the current price; the plan stays dormant unless a de-rating entry triggers.

## Entry Rules

- Enter ONLY via a $330.00 limit order on a de-rating toward the fair-value floor; do not chase at the current ~$376 price.

## Stop Rules

- Initial hard stop at $295.00 (-11% from the $330 entry), below the fair-value floor, to cap risk on a de-rating overshoot or a China/operational shock.

## Forward Guidance Reconciliation

- Status: pass
- none: No single thresholded guide metric; guide=no quantitative guide; inside=False
