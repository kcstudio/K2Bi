# Eval: manual-promote-success

## Setup

`K2Bi-Vault/wiki/watchlist/AAPL.md` does NOT exist.

LLM call is mocked to return a valid response with `quick_score: 72, rating_band: B`.

## Task

Run `python3 -m scripts.lib.invest_screen --manual-promote AAPL --reason "Free cash flow machine with buybacks"`.

## Expected output

- stdout: `AAPL manually promoted: quick_score=72, band=B`
- `AAPL.md` created with:
  - `status: screened`
  - `symbol: AAPL`
  - All 14 sub-factors present
  - `quick_score`, `quick_score_breakdown`, `rating_band`, `band_definition_version: 1`
  - Stage-1 stub fields all `null`
  - `origin: keith`
- `K2Bi-Vault/wiki/watchlist/index.md` contains `| [[AAPL]] | YYYY-MM-DD | screened |`

## Pass criteria

File created with correct structure, stub fields null, Stage-2 populated, index updated.
