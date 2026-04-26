# Eval: enrich-promoted-success

## Setup

`K2Bi-Vault/wiki/watchlist/LRCX.md` exists with Stage-1 frontmatter:
- `status: promoted`
- All required Stage-1 fields present (`symbol`, `narrative_provenance`, `reasoning_chain`, `citation_url`, `order_of_beneficiary`, `ark_6_metric_initial_scores`)
- Body contains reasoning chain and linked notes

LLM call is mocked to return a valid response with:
- `quick_score: 75`
- `rating_band: B`
- All 14 sub-factors within range and summing correctly

## Task

Run `python3 -m scripts.lib.invest_screen --enrich LRCX`.

## Expected output

- stdout: `LRCX enriched: quick_score=75, band=B`
- `LRCX.md` frontmatter now contains:
  - `status: screened`
  - `quick_score: 75`
  - `quick_score_breakdown.technical`, `.fundamentals`, `.catalyst`
  - All 14 `sub_factors.*` keys
  - `rating_band: B`
  - `band_definition_version: 1`
- All Stage-1 fields preserved byte-for-byte (same order, same whitespace)
- Body preserved verbatim
- `K2Bi-Vault/wiki/watchlist/index.md` contains `| [[LRCX]] | YYYY-MM-DD | screened |`

## Pass criteria

All expected output conditions hold; no Stage-1 key mutated.
