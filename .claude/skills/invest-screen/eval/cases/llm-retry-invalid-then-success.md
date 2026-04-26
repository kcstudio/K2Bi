# Eval: llm-retry-invalid-then-success

## Setup

`K2Bi-Vault/wiki/watchlist/CRM.md` exists with valid Stage-1 data.

LLM call is mocked to fail twice (return out-of-range sub-factor) then succeed on the third attempt.

## Task

Run `python3 -m scripts.lib.invest_screen --enrich CRM`.

## Expected output

- stdout: `CRM enriched: quick_score=<valid>, band=<valid>`
- Exit code 0
- Exactly 3 LLM calls made

## Pass criteria

Success on third attempt, exit 0, correct number of LLM calls.
