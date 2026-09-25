# Bounded SEC research pilot

This package reuses the accepted Stage 2 pipeline and dashboard. `vendor-manifest.json` records the eight unchanged source files. `pilot.py` adds a durable five-session mandate, NYSE scheduling and visible pause/completion status. It never calls a broker or runtime model and always supplies empty prices.

Runtime: `/home/k2bi/research-pilot/current`, with separate mutable `/home/k2bi/research-pilot/state`. The root-owned config contains `pilot_id`, `start_date`, and the operator-approved SEC contact. Python 3.12 and `exchange_calendars` come from the existing VPS environment.

The research timer wakes daily at 18:00 America/New_York. The wrapper skips already processed sessions and holidays, handles a missed wake once using the current retrieval timestamp, and performs no HTTP after five reserved sessions. The first manual host proof counts toward the five sessions. There are at most 15 SEC request reservations, including failures. Fresh SEC facts do not imply fresh market prices or a trade recommendation.

A process interrupted after a wrapper reservation but before retrieval is accounted as interrupted and pauses without fetching newer data under its old timestamp. A prepared receipt recovers without fetching again. Corrupt state or changed configuration/code pauses; never delete state to retry or reset a quota. New scope requires a new versioned mandate and reviewed migration.

## View and control

The desk service binds only VPS loopback port 8771. From the Mac, open the authenticated tunnel through the project transport:

```bash
bash scripts/ssh-vps.sh -f -N -L 127.0.0.1:8771:127.0.0.1:8771 -o ExitOnForwardFailure=yes
```

Then open http://127.0.0.1:8771/dashboard.html. The VPS updates independently of the Mac; the local viewing tunnel must be re-established if disconnected. No public listener is configured. The private server serves the pilot state directory to the authenticated operator.

Pause through `scripts/ssh-vps.sh 'touch /home/k2bi/research-pilot/state/PAUSED'`. A timer invocation publishes the paused status; stop the timer for immediate prevention of future invocations. Removing PAUSED after reviewing its reason allows the next due session, without retrying a completed failed session. Do not change the trading engine's `.killed` file. At completion the research timer can remain as a zero-network no-op; disable it with `systemctl disable --now k2bi-research-pilot.timer` through the root SSH route when closing observation. Viewing may remain available.

## Local verification

```bash
/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 -m unittest discover -s scripts/research_pilot -p test_pilot.py -v
```

Tests run on the Mac with mocked SEC transport and the real frozen pipeline. No tests run on the VPS. Deployment uses the exact reviewed release manifest, not the general script that restarts the trading engine. Receipts and deployment evidence live in `proposals/research-pilot-2026-09-25/`.

Integrity checks detect persistent source/configuration drift at run boundaries. They assume a root-owned, read-only release and trusted administrators. They are not a defense against an administrator replacing vendor files concurrently between verification and import. Release updates must stop the research timer, acquire the pilot lock, and use a reviewed migration; never edit an active release in place.
