# Eval: enrich-idempotent

## Setup

`K2Bi-Vault/wiki/watchlist/AMD.md` exists with `status: screened` and full Stage-2 frontmatter including `date: 2026-04-20`.

## Task

Run `python3 -m scripts.lib.invest_screen --enrich AMD` without `--re-enrich`.

## Expected output

- stdout: `AMD already screened on 2026-04-20; pass --re-enrich to overwrite`
- Exit code 0
- File contents unchanged
- No LLM call made

## Pass criteria

Exit 0, file unchanged, message matches expected format.
