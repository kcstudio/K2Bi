---
name: invest-memo
description: Produce a polished investor-facing DOCX memo after a ticker has completed K2Bi discovery, screening, thesis, claim verification, bear-case, backtest, strategy draft, and approval. Use when Keith says /memo <SYMBOL>, "investment memo", "analyst memo", "IC memo", "shareable docx", "system output document", or wants the final analysis packaged for someone evaluating a ticker.
tier: analyst
routines-ready: true
phase: 3
status: mvp
---

# invest-memo

Creates the final investor memo for a ticker that has already passed through the K2Bi research and approval cycle. The memo is a presentation-quality DOCX artifact, not a new thesis engine. It packages the existing evidence, scores, risks, backtest, strategy, and approval trail so a reader can understand the investment case and the system depth behind it.

## When to Trigger

- Keith says `/memo <SYMBOL>` or "create a memo for <SYMBOL>"
- Keith asks for an investment memo, analyst memo, IC memo, fund memo, research packet, shareable DOCX, or system output document
- Keith wants a completed ticker/strategy pipeline turned into a polished document for an investor, advisor, or investment-literate reader
- Keith points to a completed ticker, such as "the ticker G document", and asks for a full document carrying the analysis details

## When NOT to Use

- The ticker has not gone through thesis verification yet -> route to `/thesis <SYMBOL>` or `/invest-coach`
- Keith wants to discover candidates from a macro narrative -> route to `/invest-narrative`
- Keith wants a quick score/watchlist enrichment -> route to `/screen <SYMBOL>`
- Keith wants a bear-case gate -> route to `/bear <SYMBOL>`
- Keith wants to place, modify, or submit an order -> route to `/execute`
- Keith wants to approve or retire a strategy -> route to `/ship --approve-strategy`, `/ship --reject-strategy`, or `/ship --retire-strategy`

## Default Output

Write a DOCX to:

```text
K2Bi-Vault/Assets/docs/YYYY-MM-DD_K2Bi_investment-memo_<SYMBOL>.docx
```

Default audience is a person considering whether to invest in the ticker. Default depth is full, with a clear executive summary. Default tone is analyst memo, but accessible: define technical terms the first time they appear and avoid unnecessary jargon.

If Keith asks for public distribution, redact account IDs, VPS hostnames, internal filesystem paths, strategy share counts, and operational run details. If he asks for private/internal use, keep the audit trail intact.

## Inputs

Required:

- `<SYMBOL>` in uppercase, with common suffixes accepted (`BRK.B`, `0700.HK`)
- A completed ticker thesis at `K2Bi-Vault/wiki/tickers/<SYMBOL>.md`

Strongly expected:

- Watchlist or screen artifact at `K2Bi-Vault/wiki/watchlist/<SYMBOL>.md`
- Strategy spec in repo `wiki/strategies/strategy_*.md` or vault `K2Bi-Vault/wiki/strategies/strategy_*.md`
- Thesis `verification:` block with status `pass` or `operator-override`
- Bear-case verdict captured in thesis or related review output
- Backtest result and source reference from the strategy or vault notes
- Approval status from strategy frontmatter when a strategy exists

Optional:

- Raw research captures under `K2Bi-Vault/raw/research/`
- Raw filings, earnings, or news captures under `K2Bi-Vault/raw/`
- Company source URLs from the thesis source trail
- Rendered images, charts, or tables under `K2Bi-Vault/Assets/`

## Preflight

1. Resolve `K2BI_VAULT_ROOT`, defaulting to `~/Projects/K2Bi-Vault`.
2. Read `K2Bi-Vault/wiki/tickers/<SYMBOL>.md` first. Stop if missing.
3. Check the thesis freshness and `verification:` status.
4. Find related strategy specs by searching for the symbol in:
   - `wiki/strategies/`
   - `K2Bi-Vault/wiki/strategies/`
5. Read watchlist, strategy, backtest, bear-case, and approval artifacts if present.
6. If a load-bearing artifact is missing, tell Keith exactly what is missing and offer one of two paths:
   - "draft memo with gaps clearly marked"
   - "return to the missing pipeline stage"

Do not invent missing pipeline outputs. Mark missing sections as "Not yet produced by the pipeline" instead of filling them with fresh claims.

## Source Rules

- Treat existing K2Bi artifacts as the source of truth for thesis, scores, backtest, bear-case, and approval status.
- Use external web or company sources only to verify current, drift-prone facts or to cite the primary source trail already named by K2Bi.
- Any current market price, valuation multiple, executive name, filing date, or latest quarter is drift-prone. Verify it live before using it, or label it as "as captured in the K2Bi artifact dated YYYY-MM-DD".
- Every numeric claim in the memo should have one of:
  - source URL in the source trail
  - K2Bi artifact path
  - explicit "not independently refreshed for this memo" label
- Do not upgrade a draft claim into a verified claim. Preserve the pipeline's verification status.

## Memo Structure

Use this structure unless Keith asks for a shorter version:

1. **Cover**
   - Company name, ticker, exchange if known
   - Memo date
   - K2Bi pipeline stage reached
   - One-line stance: Bullish / Watch / Avoid / Inconclusive, copied from the approved artifact when available

2. **Executive Summary**
   - 5-8 bullets
   - What the company does
   - Why this may be investable now
   - Expected upside/downside framing
   - Main risks
   - What would change the view
   - Whether the strategy was approved, rejected, or still proposed

3. **Decision View**
   - Recommendation posture
   - Suitable investor type and time horizon
   - What the memo is not: not personalized financial advice, not an order instruction

4. **Company Snapshot**
   - Business model in plain English
   - Revenue or segment summary if available
   - Current market context and latest reported quarter if verified

5. **Thesis in Plain English**
   - Core thesis
   - Why the market may be mispricing it
   - What has to go right
   - What can go wrong

6. **Evidence Pillars**
   - 3-5 numbered pillars
   - For each: claim, evidence, source, implication

7. **Scoring and Quality Read**
   - Thesis score and band
   - Fundamental sub-scores
   - Explain each score in reader-friendly language

8. **Valuation and Scenario Analysis**
   - Bull / base / bear scenarios
   - Probability-weighted or expected-value view if present
   - Valuation method used by the thesis
   - Sensitivities and uncertainty

9. **Bear Case**
   - Bear-case verdict
   - Top counter-points
   - Veto conditions, if any
   - What evidence would falsify the thesis

10. **Backtest Sanity Check**
    - Strategy tested
    - Time window
    - Sharpe, max drawdown, win rate, return, and benchmark comparison when available
    - Plain-English interpretation of each metric

11. **Trade Construction**
    - Strategy status
    - Entry triggers
    - Stop, targets, time stop, and exit signals
    - Position sizing language must remain validator-owned unless the approved strategy already contains a concrete order field

12. **Monitoring Plan**
    - Next catalyst
    - Metrics to watch
    - Risk triggers
    - Review cadence

13. **K2Bi Pipeline Audit Trail**
    - Discovery source
    - Screen/watchlist output
    - Thesis verification status
    - Bear-case result
    - Backtest result
    - Strategy approval status
    - Source artifact paths

14. **Source Trail**
    - External URLs
    - K2Bi vault paths
    - "Current facts refreshed on YYYY-MM-DD" note when live verification was done

15. **Glossary**
    - Define any finance terms used: drawdown, Sharpe, valuation multiple, margin, catalyst, stop loss, base case, bear case

## Writing Standard

- Start with the answer. The executive summary should let a busy reader understand the case in under two minutes.
- Use plain English first, technical label second: "maximum peak-to-trough loss (max drawdown)".
- Separate facts, interpretation, and decision. Do not blur them.
- Avoid unexplained abbreviations.
- Avoid marketing tone. The memo should read like an analyst wrote it for an investment committee.
- Use tables where comparison matters: scenario analysis, scorecard, backtest summary, audit trail.
- Keep disclaimers brief and specific.
- Do not use em dashes.

## Document Production Workflow

1. Build a memo outline from the artifacts.
2. Confirm any missing audience/privacy/depth choices only if the user's request does not already answer them. Ask at most five concise questions.
3. Draft the memo content from pipeline artifacts.
4. Create the DOCX with a real document writer such as `python-docx`.
5. Use professional styling:
   - title page or strong first-page header
   - readable body font
   - clear section hierarchy
   - tables for structured data
   - subtle accent color, not a one-color theme
   - page numbers and document date
6. Render the DOCX to PDF or PNG pages and visually inspect it before delivery.
7. Fix layout issues before calling it complete.
8. Return the DOCX path and a short summary of what was included.

When running in Codex, prefer the bundled Documents skill render workflow. If `soffice`/LibreOffice is missing, install or ask Keith before relying on unrendered output. A DOCX that was not rendered and inspected is a draft, not final.

## Guardrails

- Never submit, stage, modify, or cancel orders.
- Never bypass the approval gate.
- Never treat memo generation as strategy approval.
- Never delete or edit `.killed`.
- Never rewrite source artifacts to make the memo cleaner.
- Never hide gaps. If a stage is missing, show the gap plainly.
- Never make personalized investment advice claims. Use "investment case", "stance", "risk view", or "strategy posture".
- Never claim facts are current unless refreshed during the memo run.

## Invocation Shape

```text
/memo G
/memo G --private
/memo G --public
/memo G --draft-with-gaps
/memo G --refresh-current-facts
```

Flags are advisory for the agent:

- `--private`: keep internal audit and operational context when useful
- `--public`: redact internal paths, account IDs, hostnames, and operational details
- `--draft-with-gaps`: produce the memo even if some pipeline stages are missing, with gaps marked
- `--refresh-current-facts`: verify current quarter, market price, valuation, and source URLs live before writing

## Completion Checklist

- DOCX exists in `K2Bi-Vault/Assets/docs/`
- Rendered pages were inspected
- Executive summary is clear and complete
- Source trail is present
- Pipeline audit trail is present
- Missing stages, if any, are marked
- Drift-prone current facts are either refreshed or clearly dated
- No em dashes
- No order action was taken
