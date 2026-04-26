# Learnings: invest-regime

## What Works

- Single-band taxonomy (crash / bear / neutral / bull / euphoria) is coarse enough to avoid false precision and rich enough to drive strategy posture decisions.
- Atomic write via `atomic_write_bytes` (tempfile + `os.replace`) guarantees readers never see a partial file, even if the process is killed mid-write.
- argparse CLI with required `--reason` enforces the reasoning discipline; no classification without justification.

## What Doesn't Work

- (To be filled as the skill runs in production)

## Patterns Discovered

- Overwrite-without-history is the right MVP trade-off: Phase 3 only needs the current regime context, not a time series. History archiving belongs in Phase 4 if operator asks for it.
- Optional `--indicators` JSON keeps the CLI scriptable while remaining human-friendly; missing keys render `n/a` rather than failing, which avoids blocking a classification when one data point is unavailable.
- `reasoning_summary` frontmatter field (first-sentence truncation) gives other skills a one-line context without parsing the full body.
