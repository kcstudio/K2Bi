# Eval: atomic-write-failure

## Setup

`K2Bi-Vault/wiki/watchlist/IBM.md` exists with valid Stage-1 data.

LLM call is mocked to return valid data.

`os.replace` is monkey-patched to raise `OSError("simulated failure")` when called for `IBM.md`.

## Task

Run `python3 -m scripts.lib.invest_screen --enrich IBM`.

## Expected output

- `OSError` raised with message "simulated failure"
- `IBM.md` contents unchanged (original Stage-1 data intact)
- No orphaned tempfile remains in `K2Bi-Vault/wiki/watchlist/`

## Pass criteria

Exception raised, original file byte-identical, no orphan temp.
