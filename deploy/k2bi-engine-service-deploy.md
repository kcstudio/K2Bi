# k2bi-engine.service operator deploy note

This repository template is the source artifact for the gated VPS unit update.
It is not applied by this build.

Operator deploy steps:

1. Copy `deploy/k2bi-engine.service` to `/etc/systemd/system/k2bi-engine.service` on the VPS after review approval.
2. Run `sudo systemctl daemon-reload`.
3. Run `sudo systemctl restart k2bi-engine.service`.
4. Verify with this copy-paste command:
   `systemctl show k2bi-engine.service -p Restart -p StartLimitIntervalSec -p StartLimitBurst -p RestartSec`
5. Verify the engine journals `engine_started` and reaches `active (running)`.

Required policy values:

- `Restart=always`
- `RestartSec=30`
- `StartLimitIntervalSec=10`
- `StartLimitBurst=5`

Rationale: clean closedown exits must restart, while a genuine sticky recovery
mismatch should still trip systemd's start-limit burst and land in `failed`
instead of looping indefinitely.
