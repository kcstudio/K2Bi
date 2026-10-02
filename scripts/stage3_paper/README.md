# Stage 3 paper snapshot (read only)

This package records and displays the IBKR paper account (DUQ220152) as a
read only snapshot. It never places orders, never requests market data, never
schedules work, and never activates anything silently.

## Capture (run by the manager)

The capture snippet runs only through the existing helper:

```
K2BI_GATEWAY_CLIENT_ID=90 scripts/gateway-query.sh -f scripts/stage3_paper/capture.py > proposals/stage3-paper-2026-10-02/paper-snapshot.json
```

It connects to `127.0.0.1:4002` with `readonly=True`, requests positions and the
account summary for DUQ220152 only, and prints a single JSON document. The
lease must be an integer from 90 to 99 and never 1. The recorded state is the
accepted EOD delivery state already produced by the Stage 2 flow.

## Render

Use the committed EOD delivery / recorded state directory, then render the
manager provided snapshot artifact at
`proposals/stage3-paper-2026-10-02/paper-snapshot.json`:

```
python3 -m scripts.stage3_paper.cli render --state-dir <state-dir> --snapshot proposals/stage3-paper-2026-10-02/paper-snapshot.json --as-of 2026-10-02T00:00:00+00:00 --output <out.html>
```

Omitting `--snapshot` renders an explicit unknown paper view. Any malformed
snapshot fails closed with exit code 2 and no partial output. The output path
must end in `.html` and must not collide with the state directory, the
original snapshot source, or any symlink into protected paths.

## Safety scope

- Strict, no coercion parsing of the recorded JSON: extra keys, wrong types,
  booleans where integers are required, NaN, exponents, duplicate JSON keys,
  future timestamps, and account mismatches all fail closed.
- Null positions or null account values mean unknown; they are never converted
  to an empty list or zero.
- A present empty positions list means known zero holdings.
- Account value currencies are displayed separately and never summed or
  converted. Null currencies or values stay unknown.
- A fixture snapshot is labelled INVENTED OFFLINE BROKER FIXTURE. A stale
  snapshot (older than 15 minutes at evaluation time) is labelled STALE and is
  never presented as current.
- The rendered page reuses the accepted EOD HTML and injects into the existing
  `panel-paper` section only. It adds no external links and never mutates the
  receipt.
