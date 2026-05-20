---
tags: [feature, burn-in, heartbeat, telegram, k2bi]
date: 2026-05-13
type: feature-tracker
origin: keith
status: shipped
priority: medium
effort: S
impact: medium
mvp: "Daily Telegram heartbeat lands at K2Bi Alerts channel at 09:00 HKT containing engine state + position summary + anomaly count for the past 24h, with anomaly details if count > 0. Binary test: simulate 1 engine_stopped event in journal, run script, expect message includes 'engine_stopped at <ts>' in anomaly section."
shipped-date: 2026-05-13
shipped-commit: 9159e66390f765ca35ec887cc1949169cf10c618
phase-3.10-started-date: 2026-05-20
up: "[[index]]"
---

# feature: burn-in daily heartbeat

Daily burn-in heartbeat for Phase 3.10 and the Phase 5 extension window.

## Shipped

- `scripts/burn-in-heartbeat.py` reads IBKR Gateway on `127.0.0.1:4002` with `clientId=99`, reads the last 24h of journal events, prints the heartbeat body to stdout, and sends the same body through `scripts/send-telegram.sh`.
- `tests/test_burn_in_heartbeat.py` covers clean days, engine bounce anomalies, broker-unreachable exit behavior, missing journal exit behavior, missing burn-in state, malformed journal rows, timestamp drift, sidecar locking, broker timeout handling, and `.env` loading for the direct Python cron command.
- VPS scripts-lane sync completed on 2026-05-13. First-run validation returned exit 0 from the VPS Python path.

## Operator Step Completed

Completed on 2026-05-20 during Phase 3.10 burn-in start. The k2bi crontab
contains exactly one daily line:


```cron
0 1 * * * /home/k2bi/Projects/K2Bi/.venv/bin/python /home/k2bi/Projects/K2Bi/scripts/burn-in-heartbeat.py >> /home/k2bi/heartbeat.log 2>&1
```

## Phase 3.10 Start Observation

Burn-in state was written at `2026-05-20T22:06:35+08:00` HKT /
`2026-05-20T14:06:35Z` UTC:

```json
{
  "day1_date": "2026-05-20",
  "status": "active",
  "phase": "3.10",
  "duration_trading_days": 5,
  "started_after_phase_g_pass_commit": "458287803c8a01dfb3a9ac83e511b9420eff7791"
}
```

Post-state `--no-send` heartbeat exited `0` and reported
`Burn-in: day 1 of 5`. Post-enable `--no-send` heartbeat exited `0` and
reported engine active with SPY qty `2` plus STP `697.13`, G flat, and
`Burn-in: day 1 of 5`.

## Live First-Run Observation

The VPS first-run heartbeat reported engine active, SPY position and stop present, G position and stop missing from the broker snapshot, 620 cycle skips, and 27 anomalies in the prior 24h. Telegram delivery succeeded after the script loaded the existing project `.env`.
