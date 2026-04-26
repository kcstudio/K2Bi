# Eval: manual-promote-without-reason

## Setup

`K2Bi-Vault/wiki/watchlist/MSFT.md` does NOT exist.

LLM call is mocked to return a valid response with `quick_score: 55, rating_band: C`.

## Task

Run `python3 -m scripts.lib.invest_screen --manual-promote MSFT` (no `--reason`).

## Expected output

- stdout: `MSFT manually promoted: quick_score=55, band=C`
- `MSFT.md` created with full Stage-2 data
- LLM prompt includes `"none provided"` as the OPTIONAL_REASON

## Pass criteria

File created successfully, LLM called with empty reason context.
