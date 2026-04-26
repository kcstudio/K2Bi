# Eval: enrich-missing-stage1-field

## Setup

`K2Bi-Vault/wiki/watchlist/TSLA.md` exists with `status: promoted` but frontmatter is missing `reasoning_chain`.

## Task

Run `python3 -m scripts.lib.invest_screen --enrich TSLA`.

## Expected output

- stderr: `error: Stage-1 validation failed for TSLA.md: missing required fields ['reasoning_chain']`
- Exit code 1
- File unchanged
- No LLM call made

## Pass criteria

Exit 1, error message names the missing field exactly, file unchanged.
