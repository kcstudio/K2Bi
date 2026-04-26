# Eval: manual-promote-refuses-overwrite

## Setup

`K2Bi-Vault/wiki/watchlist/GOOGL.md` already exists (any content).

## Task

Run `python3 -m scripts.lib.invest_screen --manual-promote GOOGL`.

## Expected output

- stderr: `error: Watchlist entry GOOGL already exists. Use --enrich if it is promoted, or --re-enrich if screened.`
- Exit code 1
- File unchanged

## Pass criteria

Exit 1, clear error message, no overwrite.
