# Eval: enrich-wrong-status

## Setup

`K2Bi-Vault/wiki/watchlist/META.md` exists with `status: draft` (not `promoted`). All required Stage-1 fields are present.

## Task

Run `python3 -m scripts.lib.invest_screen --enrich META`.

## Expected output

- stderr: `error: Stage-1 validation failed for META.md: expected status 'promoted', got 'draft'`
- Exit code 1
- File unchanged
- No LLM call made

## Pass criteria

Exit 1, error message names the wrong status, file unchanged.
