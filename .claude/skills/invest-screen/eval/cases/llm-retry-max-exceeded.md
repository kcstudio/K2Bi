# Eval: llm-retry-max-exceeded

## Setup

`K2Bi-Vault/wiki/watchlist/UBER.md` exists with valid Stage-1 data.

LLM call is mocked to always return an invalid response (component sum mismatch).

## Task

Run `python3 -m scripts.lib.invest_screen --enrich UBER`.

## Expected output

- stderr contains: `LLM scoring failed after 3 attempts`
- stderr contains the LLM's last response JSON for debugging
- Exit code 1
- File unchanged

## Pass criteria

Exit 1 after exactly 3 attempts, error includes last LLM response, file untouched.
