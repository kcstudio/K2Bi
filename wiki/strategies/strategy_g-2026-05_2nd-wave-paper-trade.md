---
tags: [strategy, g, 2nd-wave-ai-adopters, k2bi, proposed-2026-05-08]
date: 2026-05-08
type: strategy
origin: k2bi-generate
up: "[[index]]"
name: g-2026-05_2nd-wave-paper-trade
strategy_type: hand_crafted
risk_envelope_pct: 0.0025
regime_filter: []
slug: g-2026-05_2nd-wave-paper-trade
ticker: G
status: proposed
sigid: 2026-05-04-ai-agentic-2nd-wave-adopters
thesis_ref: "[[../tickers/G]]"
position_size_at_risk_pct_nav: 0.25
sizing_locked_at: t3-close
sizing_rationale: "fractional sizing per cross-vendor DR convergence + Kimi DR Option C recommendation"
backtest_status: t9_complete_sanity_gate_passed
backtest_capture_ref: "[[../../raw/backtests/2026-05-07_g-2026-05_2nd-wave-paper-trade_backtest]]"
backtest_completed_at: "2026-05-08T00:25:34+08:00"
backtest_metrics_summary:
  sharpe_annualised: 0.42
  sortino_annualised: 0.66
  max_dd_pct: -16.77
  win_rate_pct: 50.0
  avg_winner_pct: 4.87
  avg_loser_pct: -4.58
  total_return_pct: 13.86
  n_trades: 6
  avg_trade_holding_days: 38.0
  window_start: "2024-05-07"
  window_end: "2026-05-07"
  benchmark: SPY
  baseline_strategy: "lag-1 SMA(20)/SMA(50) crossover (Phase 2 MVP fixed baseline; NOT thesis bucket rules)"
  look_ahead_check: passed
backtest_verdict_against_operator_thresholds:
  sharpe_threshold: "Sharpe < -2.0 → FLAG"
  sharpe_actual: 0.42
  sharpe_flag_triggered: false
  max_dd_threshold: "Max DD > -75% → FLAG"
  max_dd_actual: -16.77
  max_dd_flag_triggered: false
  win_rate_threshold: "Win-rate vs SPY < 25% → ADVISORY"
  win_rate_actual: 50.0
  win_rate_advisory_triggered: false
  net_verdict: "PASS -- no flags or advisories triggered; sanity check confirms price action is normal-shaped (no extreme outliers); thesis can proceed to T10 strategy spec drafting"
strategy_spec_status: drafted_2026-05-08
t10_close:
  completed_at: "2026-05-08T11:18:57+08:00"
  nav_at_drafting:
    value_hkd: 1002314.18
    currency: HKD
    account_id: DUQ220152
    pulled_at_utc: "2026-05-08T03:12:00+00:00"
    pulled_via: "scripts/gateway-query.sh against VPS IBKR Gateway with clientId=99 ad-hoc operator query (per VPS-canonical path established Phase 3.9 Stage 4 2026-04-25; CLAUDE.md previously stale on 'IB Gateway on local workstation' line; updated by operator in parallel session prior to T10)"
    post_recovery_note: "post-recovery NAV after gateway was offline ~3.5h on 2026-05-08 (paper credentials expired silently after ~10-14 days no Client Portal login; recovered after operator re-login + ib-gateway.service restart)"
  usd_hkd_rate_used_at_drafting: 7.80
  usd_hkd_rate_note: "approximation; HKD pegged to USD in 7.75-7.85 band; 7.80 is band midpoint; for paper trade purposes USD/HKD fluctuation within peg band is not material"
  existing_position_at_drafting:
    ticker: SPY
    quantity: 2
    avg_price_usd: 707.72
    notional_usd: 1415.44
    notional_hkd_approx: 11040.43
    notional_pct_nav_approx: 1.101
  share_count_calculation:
    nav_hkd: 1002314.18
    risk_budget_pct_nav: 0.0025
    risk_budget_hkd: 2505.79
    risk_budget_usd_at_780_rate: 321.26
    risk_per_share_usd: 4.50
    share_count_unrounded: 71.39
    share_count_rounded_down: 71
    rounding_rationale: "round DOWN to 71 (smaller risk than budget) per discipline; 72 shares = 0.252% NAV at-risk (slightly over budget); 71 shares = 0.249% NAV at-risk (under budget; within 0.25% target)"
  ibkr_paper_account_currency_note: "IBKR HK demo paper account base currency HKD per Phase 3.9 Stage 1 setup; G is USD-denominated NYSE-listed stock; cross-currency exposure via auto-conversion at execution"
  decisions_locked_at_t10_entry:
    d1_entry_order_type: "(i) market order at next regular trading open"
    d2_stop_loss_methodology: "(c) pivot/structure-based stop just below 52-week low ($30.00; 52-week low $30.38 per Kimi DR PDF section 4.3.1)"
    d3_pyramid_partial_exit: "(i) no pyramid + no partial exit; single entry, single full exit on whichever of 4 triggers fires first"
  validator_blocker_status:
    instrument_whitelist_current: "[SPY, G]"
    g_status: whitelisted
    g_whitelist_approval_commit_sha: c73ccbf
    g_whitelist_approval_at: "2026-05-08T03:51:29.437799+00:00"
    propose_limits_delta_path: "K2Bi/review/strategy-approvals/2026-05-04_limits-proposal_instrument_whitelist-add-G.md"
    propose_limits_delta_status: "approved at c73ccbf"
    operator_command_used: "/invest-ship --approve-limits review/strategy-approvals/2026-05-04_limits-proposal_instrument_whitelist-add-G.md"
    sequencing: "completed BEFORE T12 approval gate fires per option A sequencing locked at T3 close"
order:
  ticker: G
  status: proposed
  side: buy
  qty: 71
  order_type: MKT
  time_in_force: DAY
  entry_execution: "market order at next regular trading open"
  limit_price: null
  notional_usd_at_drafting: 2449.50
  notional_hkd_at_drafting: 19106.10
  notional_pct_nav: 1.906
  entry_price_target_usd_at_drafting: 34.50
  entry_price_note: "current trading price ~$34.50 USD; market order at next regular trading open will fill at whatever the open prints"
  stop_loss: 30.00
  stop_loss_distance_per_share_usd: 4.50
  stop_loss_rationale: "just below 52-week low $30.38 per Kimi DR PDF section 4.3.1; technical stop fills the between-earnings gap with a real signal (52-week-low break = market pricing more downside than already in)"
  fair_value_target_usd: 48.18
  fair_value_target_pct_upside_at_entry: 39.65
  fair_value_target_source: "Simply Wall St analysis verified at T7 P4-T1-4 exact match"
  position_size_at_risk_pct_nav_locked_at_t3: 0.25
  position_size_at_risk_hkd: 2492.10
  position_size_at_risk_usd_at_drafting: 319.50
  position_size_at_risk_pct_nav_actual_at_71_shares: 0.2486
  ticker_concentration_validator_check: "1.906% notional well within 20% per-ticker concentration cap (max_ticker_concentration_pct in execution/validators/config.yaml)"
  trade_risk_validator_check: "0.249% at-risk well within 5% open-risk cap (max_open_risk_pct in execution/validators/config.yaml)"
  exit_triggers:
    technical_stop:
      condition: "price <= $30.00"
      execution: "STP order at $30.00"
      loss_at_trigger_usd: 319.50
      loss_at_trigger_hkd: 2492.10
      pct_nav_loss: -0.249
    timed_9_month_margin_expansion:
      condition: "cumulative observed +50bps GM AND +25bps adj op-margin progress missed by ~270 days post-entry"
      check_points: ["Q2 2026 ~80-90 days post-entry", "Q3 2026 ~180 days post-entry", "9-month gate ~270 days post-entry"]
      execution: "market order full exit regardless of price"
      anchor: "Phase 4 kill criterion (a) lived-signal T2"
    immediate_narrative_reversal:
      condition: "AI-productivity narrative reversal at scale (layoff reversals at scale; AI project failures at scale; executive removals tied to AI failures; regulatory interventions blocking AI deployments)"
      execution: "immediate market order full exit regardless of timing or unrealized loss"
      anchor: "Phase 4 kill criterion (b) lived-signal T2"
    fair_value_target:
      condition: "price >= $48.18"
      execution: "market order full exit at $48.18"
      gain_at_trigger_usd: 971.28
      gain_at_trigger_hkd: 7576.18
      pct_nav_gain: 0.756
  no_pyramid_no_partial_exit_per_d3: "Single entry at 71 shares, single full exit on whichever of 4 triggers fires first"
forward_guidance_check:
  status: override
  completed_at: "2026-05-08T11:36:55+08:00"
  override_reason: "Kill criteria (a) are deliberately keyed to the +50bps GM and +25bps op-margin guide endpoints because the thesis IS that management hits their published guide; mechanical trigger if guide breaks is the intended downside discipline, not a bug. EPS and revenue thresholds at guide floors carry the same intentional lock. ATS threshold sits below the revised-up at-least-20% floor (margin of safety)."
  thresholded_metrics:
    - metric: "GM TTM expansion +50bps to 36.5%"
      locked_threshold_text: "+50bps GM to 36.5% (Phase 4 kill criterion (a) trigger)"
      guide_source_text: "Q1 2026 Genpact earnings (PRNewswire press release; print date 2026-05-07)"
      guide_range_text: "approximately 36.5%, up approximately 50 basis points year-over-year"
      sits_inside_guide: true
      operator_note: "kill criterion (a) trigger; intentional lock"
    - metric: "Adj op-margin expansion +25bps to 17.7%"
      locked_threshold_text: "+25bps adj op-margin to 17.7% (Phase 4 kill criterion (a) trigger)"
      guide_source_text: "Q1 2026 Genpact earnings (PRNewswire press release; print date 2026-05-07)"
      guide_range_text: "approximately 17.7%, up approximately 25 basis points year-over-year"
      sits_inside_guide: true
      operator_note: "kill criterion (a) trigger; intentional lock"
    - metric: "Adj diluted EPS ~10% growth"
      locked_threshold_text: "~10% adj diluted EPS growth"
      guide_source_text: "Q1 2026 Genpact earnings (PRNewswire press release; print date 2026-05-07)"
      guide_range_text: "more than 10%"
      sits_inside_guide: true
      operator_note: "threshold at guide floor; directional operating-leverage support"
    - metric: "Total revenue growth at-least-7%"
      locked_threshold_text: "at-least-7% total revenue growth"
      guide_source_text: "Q1 2026 Genpact earnings (PRNewswire press release; print date 2026-05-07)"
      guide_range_text: "at least 7% as-reported / 6.8% constant currency"
      sits_inside_guide: true
      operator_note: "threshold at guide floor; thesis horizon support"
    - metric: "ATS high-teens revenue growth"
      locked_threshold_text: "high-teens ATS revenue growth (Phase 1 P1-T1-3 anchor)"
      guide_source_text: "Q1 2026 Genpact earnings (PRNewswire press release; print date 2026-05-07)"
      guide_range_text: "at least 20% (revised UP from high-teens)"
      sits_inside_guide: false
      operator_note: "threshold below revised-up floor; safety margin"
  source: "Q1 2026 Genpact earnings (PRNewswire press release; print date 2026-05-07)"
  source_date_correction_at_t11: "coach-side context originally referenced Q1 2026 print as April 29 2026; actual print date was May 7 2026; minor calendar correction applied at T11 close to G.md frontmatter (line 485 ongoing_milestones) + body (Catalyst Timeline table); strategy file frontmatter never used April 29 reference"
  decisions_locked_at_t11_entry:
    d1_source: "(a) Q1 2026 earnings call only -- canonical recent source; Q4 2025 already verified at T7 P3-T1-3"
    d2_threshold_scope: "(i) all 5 thresholds (GM / op-margin / EPS / revenue / ATS)"
    d3_override_visibility: "(iii) surface override only on contradiction; none surfaced"
  q1_2026_actuals_reinforcing_trajectory:
    revenue_q1: "$1.296B (+6.7% YoY) -- on track for FY guide"
    gross_margin_q1: "36.4% (+110bps vs Q1'25) -- already near FY 36.5% target"
    adj_diluted_eps_q1: "$0.98 (+16.7% YoY) -- well above 'more than 10%' guide trajectory"
    op_margin_q1: "flat in Q1 -- FY +25bps typically back-half-loaded, not contradictory"
    net_actuals_signal: "Q1 print materially supports thesis trajectory; GM Q1 already +110bps vs prior-year Q1 (above FY +50bps target trajectory); EPS Q1 +16.7% well above FY +10% guide; op-margin flat is back-half-loading per management cadence not red flag"
  contradictions_count: 0
  override_path_invoked: false
  override_path_visibility_per_d3_iii: "surface override only on contradiction; none surfaced; override path NOT INVOKED; L-2026-04-30-001 framing reserved for actual contradiction surface (not pre-emptively surfaced)"
  net_t11_verdict: "PASS -- all 5 thresholds REAFFIRMED with 1 REVISED UP (ATS to at-least-20%); Q1 2026 actuals reinforce trajectory (GM +110bps Q1 already near FY target; EPS +16.7% well above guide); kill-criterion-relevant +50bps GM / +25bps op-margin EXACT reaffirmation means Phase 4 forced-exit logic unchanged; ATS upgrade meaningfully strengthens 2nd-wave transformation thesis; ready for T12 operator approval handoff"
  ready_for_t12: true
t9_skill_invocation_note: "invest-backtest skill loaded at T9; precondition format mismatch detected (skill Step 1 expects strategy file at wiki/strategies/strategy_<slug>.md to exist; coach pipeline T9-then-T10 ordering means T9 fires BEFORE T10 produces strategy file). Adapted via Option X (operator-confirmed at T9 entry recalibration): authored this minimal placeholder pre-T10 to satisfy skill precondition. Skill backtests fixed lag-1 SMA(20)/SMA(50) crossover baseline (NOT strategy bucket rules per Phase 2 MVP) so placeholder is functionally equivalent to drafted strategy file from skill's perspective. T10 will overwrite/extend this placeholder with full bucket rules + concrete entry price + stop-loss distance + share count against live IBKR demo paper account NAV. Skill's raw/backtests/ audit-trail capture preserved. Coach skill T9-then-T10 ordering preserved."
pattern_observation_n2_skill_precondition_mismatch: "Second skill-precondition mismatch caught at coach pipeline transition (T8 invest-bear-case expected thesis_score top-level field per skill Step 2; T9 invest-backtest expects strategy_<slug>.md per skill Step 1). Both resolved via in-line adaptation with explicit documentation. Pattern is now N=2; flag at /ship close audit summary as recurring coach-skill / individual-skill ordering interface issue (not blocking; pattern documents the interface gap; future architect Q-entry candidate alongside Q37+ vendor-confabulation discipline)."
---

# Strategy: G 2nd-wave AI adopters paper trade

**Status: `proposed` (T10 drafted 2026-05-08; pending T11 forward-guidance check + T12 operator approval via `/invest-ship --approve-strategy`).**

K2Bi Phase 3.8b first paper trade. Single-name long-only position in Genpact (NYSE: G) at 0.25% NAV at-risk fractional sizing. NAV pulled from VPS IBKR Gateway HKD 1,002,314.18 (account DUQ220152) at 2026-05-08T03:12:00 UTC. Entry: **71 shares at market order next regular trading open** (~$34.50 USD). Stop: **$30.00 USD** (just below 52-week low $30.38). Fair-value target: **$48.18 USD** (~40% implied upside). Single entry, single full exit on whichever of 4 exit triggers fires first.

## How This Works (Plain English)

K2Bi is buying Genpact (NYSE: G) on the bet that the company is a "2nd-wave AI adopter" -- a non-tech mid-cap that is converting its own delivery model from human labor to AI agents, then selling that capability to other non-tech enterprises facing the same conversion challenge. The thesis is that Genpact's stock has fallen ~30% over the past year because investors haven't recognized this transformation yet. We're entering near 52-week lows ($34.50) before the transformation validates publicly.

### Plain-English glosses for terms used below

- **basis points (bps)** — one-hundredth of a percentage point. "+50 basis points" = "+0.50 percentage points". See [[../reference/glossary#basis-points]] (stubbed).
- **P/E ratio (price-to-earnings)** — stock price divided by per-share earnings; how many years of current earnings the price represents. See [[../reference/glossary#p/e]].
- **trailing P/E** — uses the past 12 months of earnings (what the company actually earned). **Forward P/E** uses the next 12 months of analyst-estimated earnings (what they're expected to earn). Forward is lower when growth is expected. See [[../reference/glossary#forward p/e]].
- **re-rate** — when the market changes how it values the same earnings stream upward (P/E expands) or downward (P/E contracts). A "re-rate to $48" means the market starts paying a higher P/E multiple, even if earnings don't change. See [[../reference/glossary#re-rate]] (stubbed).
- **notional** — the dollar size of a position computed as price × shares (the "face value" you control), separate from how much you stand to lose if it goes against you. See [[../reference/glossary#notional]] (stubbed).
- **STP order (stop order)** — a resting "if price drops to X, sell at market" order pre-placed with the broker so the exit fires automatically without manual action. See [[../reference/glossary#stp-order]] (stubbed).
- **NAV at risk** — the dollar amount you would actually lose if your stop-loss triggers cleanly, expressed as a fraction of your total account value. Distinct from notional (position size). 0.25% NAV at risk on this account = $319.50 USD ≈ HKD 2,492 ≈ roughly one moderate dinner-out for two on the paper account.
- **Sharpe / Sortino / drawdown / SMA crossover / momentum / walk-forward** — see Backtest section below; all glossed there on first use.

### What we're betting on

1. **Margin expansion delivers as guided.** Genpact has guided 2026 gross margin to expand by 50 basis points (+0.50 percentage points; 36.0% → 36.5%) and adjusted operating margin by 25 basis points (+0.25 percentage points; 17.5% → 17.7%). If they hit these numbers in Q2/Q3 2026 earnings, the AI-conversion thesis is validating in financial reality.
2. **Cheap valuation is "narrative neglect" not "fundamental deterioration."** The stock trades at trailing P/E ~11x (price is 11 years of past earnings) / forward P/E ~8.6x (price is 8.6 years of next-12-months expected earnings) — corrected at T7 P4-T1-4. If the AI conversion is real, the stock can re-rate (market pays a higher P/E multiple for the same earnings) from $34.50 toward Simply Wall St fair-value estimate ~$48.18 (40% upside).
3. **Position size is intentionally small** (0.25% of account NAV at risk = HKD 2,492 ≈ USD 320 ≈ roughly one moderate dinner-out for two on this paper account). This is a contrarian thesis with real risks; sizing reflects "small win or small loss" not "big bet on confident thesis."

### What we're protecting against (bounded downside via 4 exit triggers; whichever fires first)

1. **Technical stop at $30.00.** If Genpact stock breaks below its 52-week low ($30.38), the market is signaling more downside than the thesis assumes. We exit at $30.00 immediately. Loss is capped at 71 × $4.50 = $319.50 USD ≈ HKD 2,492 ≈ 0.249% NAV at-risk -- exactly the 0.25% budget we committed at T3 close.
2. **9-month timed exit.** If Genpact misses the +50bps GM / +25bps op-margin guide for two consecutive quarterly prints (Q2 + Q3 2026), the transformation isn't validating as expected. We exit at ~270 days post-entry regardless of price (Phase 4 kill criterion (a); lived-signal T2 anchor).
3. **Immediate AI-productivity narrative-reversal exit.** If the broader "AI productivity" narrative reverses (companies publicly rehire after AI-driven layoffs, AI projects fail at scale, executives get removed for AI failures, regulators block AI), the entire 2nd-wave thesis is broken. Immediate exit (Phase 4 kill criterion (b); lived-signal T2 anchor).
4. **Fair-value target $48.18.** If Genpact reaches Simply Wall St fair-value estimate $48.18, we exit and lock in the gain (71 × $13.68 = $971.28 USD ≈ HKD 7,576 ≈ 0.756% NAV gain).

### The single most important number to monitor

**Q2 2026 Genpact earnings (~July-August 2026, ~80-90 days post-entry).** If they hit +50bps GM / +25bps op-margin trajectory, the thesis is validating. If they miss, the 9-month timed exit clock starts counting down to the ~Feb 2027 forced exit.

### Why this position size is small

At HKD 2,492 at-risk on ~HKD 1,002,314 NAV account, this is intentionally a learning-size first paper trade for K2Bi Phase 3.8b. The thesis has real uncertainty:

- Bear-case probability 27% per asymmetry analysis (T6 sub-score 5)
- Bear-case Claude Code call (T8) returned PROCEED at 55% bear conviction (below 70% VETO) but flagged 3 active monitoring items
- Mixed peer-cohort evidence on use-case-capture-compounding (T8 finding: Infosys + TCS + Cognizant deepening; Wipro declining; EXLS positive small-scale)
- Platform-player threats from ServiceNow Now Assist + Salesforce Agentforce + Workday Illuminate are SHIPPING, not future (T8 finding: Door B is OPEN now, not opening)
- Demand-side validation scale corrected at T7 to ~$80M AP-specific (vs ~$2.2B implied before correction)

Future trades can be larger if this paper trade validates the thesis-building process AND the underlying thesis. For now: small win or small loss with bounded downside via 4 named exit triggers + position-level validator caps (1% per-trade / 20% per-ticker concentration / 5% trade-risk / market-hours / cash-only).

### Currency note (HKD-base account holding USD-denominated stock)

The IBKR HK demo paper account is HKD-base (NAV HKD 1,002,314.18 per pull at 2026-05-08T03:12 UTC). G is USD-denominated NYSE-listed stock. IBKR auto-converts HKD ↔ USD at execution. For a 71-share position at $34.50 USD (~$2,449 USD notional ~HKD 19,106), USD/HKD fluctuation within the 7.75-7.85 peg band creates ~$25 USD of cross-currency exposure -- not material at this position size.

## Strategy bucket rules

Single entry, single full exit on whichever of 4 exit triggers fires first. No pyramid + no partial exit (operator D3 i locked at T10 entry).

### ENTRY

| Field | Value |
|---|---|
| Type | Market order (MKT) |
| Side | Buy |
| Ticker | G |
| Quantity | **71 shares** |
| Time in force | DAY |
| Execution | At next regular trading open (US market hours 09:30 ET) |
| Entry price target | ~$34.50 USD (current trading price; market order fills at whatever the open prints) |
| Notional | $2,449.50 USD ≈ HKD 19,106 ≈ 1.906% of NAV |
| At-risk | $319.50 USD ≈ HKD 2,492 ≈ 0.249% of NAV |
| Validator checks | 1.906% notional within 20% per-ticker concentration cap; 0.249% at-risk within 5% open-risk cap |

### EXIT TRIGGERS (any one fires → full exit of all 71 shares)

**Trigger 1 -- Technical stop (52-week-low break):**
- **Condition:** price <= $30.00
- **Execution:** STP order (stop order; pre-placed "sell at market if price drops to X" instruction with the broker) at $30.00 (resting stop-loss order placed at entry)
- **Loss at trigger:** 71 × ($34.50 - $30.00) = $319.50 USD ≈ HKD 2,492 ≈ -0.249% NAV
- **Rationale:** $30.00 is just below the 52-week low ($30.38 per Kimi DR PDF section 4.3.1). A break of 52-week low signals market pricing more downside than the thesis assumes. Fills the between-earnings gap with a real signal at minimal cost.

**Trigger 2 -- Timed 9-month margin-expansion exit (Phase 4 kill criterion (a); lived-signal T2 anchor):**
- **Condition:** cumulative observed +50bps GM AND +25bps adj op-margin progress missed by ~270 days post-entry
- **Check points:**
  - Q2 2026 earnings ~80-90 days post-entry: first measurable progress check
  - Q3 2026 earnings ~180 days post-entry: second measurable progress check
  - 9-month gate ~270 days post-entry: forced exit if cumulative still flat
- **Execution:** market order full exit regardless of price; no price-based hold-on-decline overrides
- **Rationale:** Lived-signal T2 anchor + Phase 3 +50bps GM + +25bps op-margin guide as concrete validation target. 9-month window allows one full earnings cycle plus partial visibility into the subsequent cycle.

**Trigger 3 -- Immediate AI-productivity narrative-reversal exit (Phase 4 kill criterion (b); lived-signal T2 anchor):**
- **Condition:** any one of:
  - Layoff reversals at scale (major companies publicly rehiring in categories previously reduced for "AI efficiency")
  - AI project failures at scale (public rollbacks of deployed AI systems)
  - Executive removals tied to AI transformation failures
  - Regulatory interventions blocking AI deployments
- **Execution:** immediate market order full exit regardless of timing or unrealized loss
- **Rationale:** This trigger destroys the entire 2nd-wave thesis regardless of Genpact-specific performance.

**Trigger 4 -- Fair-value target hit (gain locked at thesis fair value):**
- **Condition:** price >= $48.18
- **Execution:** market order full exit at $48.18 (could pre-place LMT order at $48.18 OR monitor + manual fire)
- **Gain at trigger:** 71 × ($48.18 - $34.50) = $971.28 USD ≈ HKD 7,576 ≈ +0.756% NAV
- **Rationale:** Simply Wall St fair-value estimate $48.18 verified at T7 P4-T1-4 exact match. Anchored to thesis 40% implied upside framing.

### Validator-enforced caps (cannot be overridden)

| Cap | Value | Status for this trade |
|---|---|---|
| `position_size.max_ticker_concentration_pct` | 20% | 1.906% G + 1.101% existing SPY = 3.007% total -- well within |
| `trade_risk.max_open_risk_pct` | 5% | 0.249% G at-risk; SPY at-risk unknown but small absolute size -- well within |
| `leverage.cash_only` | true | Buy order with cash settlement; no margin |
| `market_hours` | 09:30-16:00 ET | DAY order at next regular open within market hours |
| `instrument_whitelist` | [SPY, G] | **G WHITELISTED** at commit `c73ccbf` (2026-05-08T03:51 UTC); propose-limits delta at K2Bi/review/strategy-approvals/2026-05-04_limits-proposal_instrument_whitelist-add-G.md cleared via `/invest-ship --approve-limits` earlier this session |

## Backtest

`t9_complete_sanity_gate_passed`. Captured at [[../../raw/backtests/2026-05-07_g-2026-05_2nd-wave-paper-trade_backtest]] (run timestamp `2026-05-07T16:24:30+00:00` UTC = `2026-05-08T00:24:30+08:00` HKT; filename uses UTC date convention per skill).

### Metrics (2024-05-07 to 2026-05-07; lag-1 SMA(20)/SMA(50) crossover baseline vs SPY benchmark)

**Plain-English glosses for terms in this table:**
- **SMA crossover** — Simple Moving Average crossover. A mechanical buy-when-short-average-crosses-above-long-average, sell-when-it-crosses-below rule. Used here only as a "is the price action well-formed" sanity check, NOT as the actual trading thesis. See [[../reference/glossary#crossover]] (stubbed).
- **lag-1 SMA(20)/SMA(50)** — buy/sell signal computed using yesterday's 20-day and 50-day averages, executed at today's open; the lag-1 prevents a look-ahead bias trap (using today's data to trade today's open).
- **Sharpe ratio** — annual return divided by annual volatility; "how much return per unit of risk taken". Higher is better; >1 is decent, >2 is great. See [[../reference/glossary#sharpe-ratio]].
- **Sortino ratio** — like Sharpe but only counts downside volatility (penalises losing days, not winning days); typically higher than Sharpe for the same strategy. See [[../reference/glossary#sortino-ratio]].
- **drawdown** — peak-to-trough decline in account value; "how bad does the worst rough patch feel?". See [[../reference/glossary#drawdown]].
- **momentum** — strategy that bets price trends continue (buy what's been going up). The thesis here is the OPPOSITE — buy on decline because we think the decline is mispricing not a real trend. See [[../reference/glossary#momentum]] (stubbed).
- **walk-forward harness** — backtest method that re-trains the strategy on each rolling time window to detect overfitting; not yet shipped in K2Bi (Phase 4+). See [[../reference/glossary#walk-forward-validation]].

| Metric | Value | Read |
|---|---|---|
| Sharpe (annualised) | **0.42** | weak risk-adjusted return for SMA crossover; not a thesis indicator (thesis is anti-momentum, not SMA crossover) |
| Sortino (annualised) | 0.66 | downside-only adjusted Sharpe; mid-low |
| Max drawdown | **-16.77%** | mild drawdown (worst 2-year point 16.77% below peak); far from -75% flag threshold |
| Win rate | 50.00% | balanced; SMA crossover expected ~50% |
| Avg winner | +4.87% | |
| Avg loser | -4.58% | symmetric trade outcomes |
| Total return | **+13.86%** | positive over 2-year window despite -30% YoY decline |
| Trades | 6 | reasonable cadence (not over-trading) |
| Avg trade holding | 38.0 days | short-medium term momentum trades |

### Verdict against operator thresholds (D3 iii permissive)

| Threshold | Actual | Triggered? |
|---|---|---|
| Sharpe < -2.0 → FLAG | 0.42 | NO -- well above flag threshold |
| Max DD > -75% → FLAG | -16.77% | NO -- far from flag threshold |
| Win-rate vs SPY < 25% → ADVISORY | 50.0% | NO -- balanced |

**Net verdict:** PASS. No flags or advisories triggered. Sanity check confirms price action is normal-shaped (no extreme outliers like fraud-event price collapse). Sanity gate (look-ahead bias check) also passed. Thesis can proceed to T10 strategy spec drafting.

### Important framing note

The SMA crossover baseline is **NOT the thesis**. The thesis is anti-momentum entry-on-decline at ~$34.50 with 12-18mo horizon + 9-month timed margin-expansion exit + immediate AI-productivity narrative-reversal exit. The SMA crossover is the skill's Phase 2 MVP fixed baseline (Phase 4 will replace with rule extraction from `## Strategy bucket rules` once walk-forward harness ships).

What this backtest tells us:
1. Sanity gate passed -- no look-ahead bias / unrealistic returns / unrealistic DD / unrealistic win-rate; mechanical signal-test is well-formed
2. Price action is normal-shaped -- max DD -16.77% over 2 years suggests intermittent rallies during the broader -30% YoY decline; no catastrophic event in the window
3. No outlier warnings -- operator's permissive thresholds NOT tripped; no genuine outlier behavior in 2-year price action

What this backtest does NOT tell us:
- Whether the thesis is right or wrong (thesis is fundamental + qualitative; backtest is mechanical price-action sanity check)
- Whether the bucket rules at T10 will produce different metrics (Phase 2 MVP doesn't extract bucket rules)
- Whether anti-momentum entry-on-decline will validate (T9 is sanity check; T11 forward-guidance + ongoing monitoring of thesis carry-forwards will tell us)

## Linked notes

- [[../tickers/G]] -- T7-verified thesis + T8 bear-case PROCEED at 55% conviction
- [[../context/context_2026-05-04-ai-agentic-2nd-wave-adopters-lived-signal]] -- T1+T2 lived-signal spine
- [[../macro-themes/theme_2026-05-04-non-tech-mid-cap-ai-adopters-cross-vendor-dr]] -- T3 cross-vendor DR discovery surface
- [[../watchlist/G]] -- Stage-1 + Stage-2 metadata + T4 manual screen Quick Score
- [[index]]
