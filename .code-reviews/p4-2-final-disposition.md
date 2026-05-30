# P4-2 Final Disposition

Latest review: `.code-reviews/2026-05-30T16-15-47Z_8588a2.log`

Verdict: NEEDS-ATTENTION, not cleared by reviewer

PM ruling: override recommended because latest findings are non-material or already covered by P4-2 design.

## Dispositions

1. Missing local journal files -> REJECT AS FALSE POSITIVE. If VPS is reachable/current and local has no journal rows, classification as `syncthing_lag` with unknown local fields is intended.

2. Hardcoded VPS root -> ACCEPT AS INTENTIONAL. The direct VPS read targets the known Hostinger K2Bi vault root; drift fails closed through existing FAIL classifications.

3. `ssh_script`/template replacement -> REJECT AS NON-MATERIAL. `shell=False` prevents shell injection, marker is per-invocation and length-framed; no P4-2 runtime risk shown.

4. session-start WARN nonblocking -> PM-RATIFIED. Manual `step0` is the evidence gate; session-start is transcript-visible early warning.

## Verification Evidence

- Focused detector tests passed.
- CLI tests passed.
- Heartbeat/alert slice passed.
- Full pytest passed.
- `deploy_config` preflight passed.

## Scope Preserved

- No PreToolUse.
- No cache/state.
- No cron/LaunchAgent/Telegram.
- No VPS helper.
- No `scripts/` implementation.
- No engine/broker/P4-1 touch.
