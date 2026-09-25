#!/usr/bin/env python3
"""Five-session SEC research pilot. No quotes, orders or runtime model calls."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / 'vendor/stage2-cycles-2026-09-20/integrated'
NY = ZoneInfo('America/New_York')
LIMITS = {'max_cycles': 5, 'max_source_requests': 15}
EXPECTED_VENDOR = {
    'vendor/stage2-cycles-2026-09-20/integrated/' + n + '.py'
    for n in ('pipeline', 'sources', 'contracts', 'research', 'ledger', 'desk')
} | {'vendor/stage2-cycles-2026-09-20/cycle.py',
     'vendor/offline-milestone-2026-09-20/report_view.py'}


def atomic(path: Path, data: bytes) -> None:
    """Replace a file durably while holding the pilot lock."""
    fd, name = tempfile.mkstemp(prefix='.pilot-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read(path: Path):
    return json.loads(path.read_text())


def write(path: Path, obj) -> None:
    atomic(path, json.dumps(obj, sort_keys=True, allow_nan=False).encode())


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_config(cfg: dict) -> dict:
    if not isinstance(cfg, dict) or set(cfg) != {'pilot_id', 'start_date', 'sec_contact'}:
        raise ValueError('Invalid pilot configuration fields')
    if cfg['pilot_id'] != 'us-sec-2026-09-25':
        raise ValueError('Unknown pilot mandate')
    start = cfg['start_date']
    if not isinstance(start, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', start):
        raise ValueError('Start date must be YYYY-MM-DD')
    dt.date.fromisoformat(start)
    contact = cfg['sec_contact']
    if (not isinstance(contact, str) or len(contact) > 254
        or not re.fullmatch(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+', contact)
        or any(x in contact.lower() for x in ('example.', 'placeholder', 'noreply'))):
        raise ValueError('A valid SEC contact is required')
    return cfg


def verify_code() -> dict:
    manifest = read(ROOT / 'vendor-manifest.json')
    if set(manifest) != EXPECTED_VENDOR:
        raise ValueError('Vendor manifest paths changed')
    for name, item in manifest.items():
        if sha(ROOT / name) != item['sha256']:
            raise ValueError(f'Vendor integrity failure: {name}')
    return {'pilot': sha(Path(__file__)), 'vendor': sha(ROOT / 'vendor-manifest.json')}


def modules():
    if str(VENDOR) not in sys.path:
        sys.path.insert(0, str(VENDOR))
    import pipeline
    import sources
    return pipeline, sources


def calendar():
    import exchange_calendars
    return exchange_calendars.get_calendar('XNYS')


def eligible(now: dt.datetime, start: str, first: bool = False) -> str | None:
    local = now.astimezone(NY)
    target = local.date()
    cal = calendar()
    if first:
        if not cal.is_session(target.isoformat()):
            return None
    elif local.hour < 18:
        target -= dt.timedelta(days=1)
    session = cal.date_to_session(target.isoformat(), direction='previous').date().isoformat()
    return session if session >= start else None


def safe_state(path: Path) -> Path:
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError('State path contains a symlink')
    path.mkdir(parents=True, exist_ok=True)
    if any(p.is_symlink() for p in path.rglob('*')):
        raise ValueError('State contains a symlink')
    if path.resolve() == ROOT or ROOT in path.resolve().parents:
        raise ValueError('State must be outside the code release')
    return path


def corpus(pipeline, state: Path):
    history = state / 'history'
    if not history.exists():
        return [], None
    with pipeline._lock_and_validate(history):
        receipts, pending, _, _ = pipeline._load_corpus(history, LIMITS)
    return receipts, pending


def validate_slots(slots, cfg, now, receipts, pending) -> list:
    """Bind each reserved session to exactly one immutable pipeline request."""
    if not isinstance(slots, dict) or len(slots) > 5:
        raise ValueError('Invalid reserved sessions')
    previous = None
    for day, req in sorted(slots.items()):
        if (not isinstance(day, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day)
            or not calendar().is_session(day) or day < cfg['start_date']):
            raise ValueError('Invalid session date')
        if not isinstance(req, dict) or set(req) != {'cycle_id', 'source_mode', 'prices', 'synthetic_prices', 'session_close'}:
            raise ValueError('Invalid saved request')
        stamp = dt.datetime.fromisoformat(req['cycle_id'])
        if (stamp.tzinfo is None or stamp.utcoffset() != dt.timedelta(0)
            or stamp.isoformat() != req['cycle_id'] or stamp > now
            or day > stamp.astimezone(NY).date().isoformat()
            or (previous is not None and stamp <= previous)):
            raise ValueError('Invalid request time or ordering')
        if req != request(stamp):
            raise ValueError('Research-only request changed')
        previous = stamp
    by_id = {r['cycle_id']: day for day, r in slots.items()}
    committed = {r['cycle_id'] for r in receipts}
    if not committed.issubset(by_id):
        raise ValueError('Receipt without reserved session')
    # Corpus already verifies request hashes, chain, code, calculations and caps.
    for rec in receipts:
        req = slots[by_id[rec['cycle_id']]]
        if rec['request_sha256'] != hashlib.sha256(json.dumps(req, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest():
            raise ValueError('Receipt request differs from reservation')
    unfinished = [day for day in sorted(slots) if slots[day]['cycle_id'] not in committed]
    if len(unfinished) > 1 or (unfinished and unfinished[-1] != max(slots)):
        raise ValueError('Multiple or out-of-order unfinished sessions')
    if pending and (not unfinished or pending[1]['request'] != slots[unfinished[0]]):
        raise ValueError('Pending intent differs from reservation')
    if receipts and receipts[-1]['state']['resources']['cycles'] != len(receipts):
        raise ValueError('Unexpected budget history')
    return unfinished


def request(now: dt.datetime) -> dict:
    return {'cycle_id': now.isoformat(), 'source_mode': 'sec', 'synthetic_prices': True,
            'prices': {}, 'session_close': False}


def publish(state, now, status, slots, receipts, errors=()):
    last_success = next((r['cycle_id'] for r in reversed(receipts)
                         if all(r.get('source_bundle', {}).get(s, {}).get('status') == 'ok'
                                for s in ('G', 'CALX', 'CDNS'))), None)
    result = {'status': status, 'checked_at': now.isoformat(), 'session': max(slots, default=None),
              'sessions_used': len(slots), 'sessions_remaining': 5-len(slots),
              'requests_reserved': 3*len(slots), 'requests_remaining': 15-3*len(slots),
              'last_successful_retrieval': last_success, 'errors': list(errors), 'runtime_model_usd': 0}
    if status == 'integrity_error':
        for key in ('sessions_used', 'sessions_remaining', 'requests_reserved', 'requests_remaining'):
            result[key] = None
    write(state / 'status.json', result)
    labels = {'ready': 'Research updated', 'waiting': 'Waiting for next session',
              'pilot_complete': 'Five-session pilot complete', 'paused': 'Pilot paused',
              'integrity_error': 'Pilot paused: verification failed'}
    escape = html.escape
    allowance = f'Sessions: {len(slots)}/5 · SEC requests reserved: {3*len(slots)}/15' if status != 'integrity_error' else 'Allowance unavailable until integrity is restored'
    banner = ("<section style='padding:20px;background:#fff7df;color:#252525;border-bottom:2px solid #d9ac37'>"
              f"<h2>{escape(labels.get(status, status))}</h2>"
              "<p>Research-only pilot: actual SEC filings. No market prices or new trades. Portfolios are simulated.</p>"
              f"<p>{escape(allowance)} · Runtime model cost: $0</p>"
              f"<p>New York session: {escape(str(result['session'] or 'not started'))} · Last successful retrieval: {escape(last_success or 'none')} (UTC)</p>"
              f"<p>{escape('; '.join(errors))}</p></section>")
    original = state / 'history/dashboard.html'
    body = original.read_text() if original.exists() else '<!doctype html><html><body><h1>K2Bi Research desk</h1></body></html>'
    # Only presentation labels change; frozen evidence/accounting stay intact.
    for before, after in {
        'Saved simulation snapshot, not a live daily service.': 'Scheduled research-only pilot. This page shows the latest saved run.',
        'Scenario time:': 'Research cycle time:',
        'Quotes and fills are synthetic.': 'No market quotes or new fills are supplied in this pilot.',
        'Stage 2 local simulation. Quotes and fills are synthetic;': 'Stage 2 research-only pilot. No market quotes or new fills;',
        'Stage 2 local simulation': 'Stage 2 research-only pilot',
        'Historical research + synthetic prices. Simulation only.': 'Actual SEC filing evidence. No market prices or new simulated trades.',
    }.items():
        body = body.replace(before, after)
    atomic(state / 'dashboard.html', body.replace('<body>', '<body>'+banner, 1).encode())
    return result


def run(config_path, state_dir, now=None, first_proof=False):
    """Perform at most one saved or newly due research cycle."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if not isinstance(now, dt.datetime) or now.tzinfo is None:
        raise ValueError('An aware timestamp is required')
    now = now.astimezone(dt.timezone.utc)
    state = safe_state(Path(state_dir).absolute())
    with (state / '.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status': 'busy'}
        slots, receipts = {}, []
        try:
            cfg = validate_config(read(Path(config_path)))
            seal = {'config': cfg, 'code': verify_code(), 'limits': LIMITS}
            pipeline, sources = modules()
            sources.UA = {'User-Agent': f"K2BiResearchPilot/1.0 {cfg['sec_contact']}"}
            control_path = state / 'control.json'
            if control_path.exists():
                control = read(control_path)
                if not isinstance(control, dict) or set(control) != {'seal', 'slots'} or control['seal'] != seal:
                    raise ValueError('Sealed configuration or code changed')
                slots = control['slots']
            else:
                if set(p.name for p in state.iterdir()) - {'.lock'}:
                    raise ValueError('Control missing from existing state; no reset allowed')
                write(control_path, {'seal': seal, 'slots': slots})
            receipts, pending = corpus(pipeline, state)
            unfinished = validate_slots(slots, cfg, now, receipts, pending)
            if (state / 'PAUSED').exists():
                reason = (state / 'PAUSED').read_text().strip() or 'Paused by operator'
                return publish(state, now, 'paused', slots, receipts, [reason])
            if len(receipts) == 5:
                atomic(state / 'COMPLETE', b'Five reserved sessions completed\n')
                return publish(state, now, 'pilot_complete', slots, receipts)
            if (state / 'COMPLETE').exists():
                raise ValueError('Completion marker does not match receipts')
            day = unfinished[0] if unfinished else eligible(now, cfg['start_date'], first_proof and not slots)
            if not unfinished:
                if day is None or day in slots:
                    return publish(state, now, 'waiting', slots, receipts)
                if slots and day <= max(slots):
                    raise ValueError('Clock moved behind reserved session')
                slots[day] = request(now)
                validate_slots(slots, cfg, now, receipts, pending)
                write(control_path, {'seal': seal, 'slots': slots})
            if unfinished and pending is None:
                # The wrapper reserved this slot before a process died, but no
                # retrieval intent exists. Charge it without fetching later data
                # under the old timestamp, then recover as interrupted.
                try:
                    pipeline.run_cycle(slots[day], state / 'history', limits=LIMITS, crash='after_intent')
                except pipeline.Crash:
                    pass
            terminal = pipeline.run_cycle(slots[day], state / 'history', limits=LIMITS)
            receipts, pending = corpus(pipeline, state)
            validate_slots(slots, cfg, now, receipts, pending)
            errors = [f"{symbol}: {item.get('reason', 'Source failed')}" for symbol, item in terminal['source_bundle'].items() if item.get('status') == 'failed']
            if terminal['status'] == 'interrupted':
                errors.append('Interrupted retrieval reserved its quota; review before resuming')
            if errors:
                atomic(state / 'PAUSED', ('\n'.join(errors)+'\n').encode())
                return publish(state, now, 'paused', slots, receipts, errors)
            if len(receipts) == 5:
                atomic(state / 'COMPLETE', b'Five reserved sessions completed\n')
            return publish(state, now, 'pilot_complete' if len(receipts) == 5 else 'ready', slots, receipts)
        except Exception as exc:
            message = f'{type(exc).__name__}: {exc}'
            atomic(state / 'PAUSED', message.encode())
            # Damaged metadata cannot be used to claim a remaining allowance.
            safe_slots = slots if isinstance(slots, dict) and len(slots) <= 5 else {}
            return publish(state, now, 'integrity_error', safe_slots, receipts, [message])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--state', required=True)
    parser.add_argument('--first-proof', action='store_true')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    try:
        if args.check:
            validate_config(read(Path(args.config)))
            verify_code()
            modules()
            calendar()
            result = {'status': 'check_ok'}
        else:
            result = run(args.config, args.state, first_proof=args.first_proof)
        print(json.dumps(result))
        return 2 if result['status'] == 'integrity_error' else 0
    except Exception as exc:
        print(json.dumps({'status': 'error', 'error': str(exc)}))
        return 2


if __name__ == '__main__':
    sys.exit(main())
