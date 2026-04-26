# Eval: enrich-re-enrich

## Setup

`K2Bi-Vault/wiki/watchlist/NVDA.md` exists with `status: screened` and existing Stage-2 data (`quick_score: 60, rating_band: C`).

LLM call is mocked to return a valid response with `quick_score: 85, rating_band: A`.

## Task

Run `python3 -m scripts.lib.invest_screen --enrich NVDA --re-enrich`.

## Expected output

- stdout: `NVDA enriched: quick_score=85, band=A`
- `NVDA.md` frontmatter updated to new Stage-2 values
- `status` remains `screened`
- Stage-1 fields still preserved byte-for-byte
- Exit code 0

## Pass criteria

New Stage-2 values written, Stage-1 untouched, exit 0.
