#!/usr/bin/env python3
"""
Integrated demo pipeline: filesystem-backed cycle ledger with sealed terminals.

Public surface:
  run_cycle(request, output, limits=None, crash=None) -> terminal dict
  report_only(output) -> latest terminal dict

Stdlib only. Uses contracts.validate_request, source.download, research.analyze,
ledger.initial_state/advance/reconcile, desk.render, and cycle._write_atomic.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

CODE_VERSION = "pipeline-2026-09-20.1"
CANONICAL_SEPARATORS = (",", ":")
DEFAULT_LIMITS = {"max_cycles": 100, "max_source_requests": 30}
UNIVERSE = ["G", "CALX", "CDNS", "SPY", "XLV"]
MODE_FIXTURE = "fixture"
MODE_SEC = "sec"


class CycleError(ValueError):
    """Raised for invalid requests, limits, or corpus state."""


class Crash(RuntimeError):
    """Injected crash for determinism tests."""


def _load_parent_write_atomic():
    parent = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("parent_cycle", parent / "cycle.py")
    if spec is None or spec.loader is None:
        raise CycleError("cannot load parent cycle.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fn = getattr(mod, "_write_atomic", None)
    if fn is None:
        raise CycleError("parent cycle.py missing _write_atomic")
    return fn


_write_atomic = _load_parent_write_atomic()


def _canonical(obj) -> str:
    return json.dumps(
        obj,
        sort_keys=True,
        separators=CANONICAL_SEPARATORS,
        ensure_ascii=False,
        allow_nan=False,
    )


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sanitize(obj):
    """Make arbitrary input JSON-safe for rejection artifacts."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return repr(obj)


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(str(path), _canonical(obj).encode("utf-8"))


def _read_json(path: Path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)



def _fingerprint():
    """Bind persisted transactions to the exact computation implementation."""
    root = Path(__file__).resolve().parent
    names = ('contracts.py', 'sources.py', 'research.py', 'ledger.py', 'pipeline.py')
    return _sha256_text(_canonical({n: _sha256_bytes((root / n).read_bytes()) for n in names}))


def _check_output_path(output):
    path = Path(output).resolve()
    here = Path(__file__).resolve().parent
    repo = here.parents[2]
    allowed = (here.parent / 'integrated-demo', here.parent / 'integrated-sec-demo')
    for protected in (repo, repo.with_name('K2Bi-Vault'), repo.with_name('K2B-Vault')):
        if path == protected or path in protected.parents:
            raise CycleError('protected output root')
        if protected in path.parents and not any(path == a or a in path.parents for a in allowed):
            raise CycleError('output inside protected code or vault')
    # Never follow an existing output-internal symlink into protected artifacts.
    if path.exists() and any(p.is_symlink() for p in path.rglob('*')):
        raise CycleError('output contains symlink')
    return path


def _check_limits(limits):
    if limits is None:
        return dict(DEFAULT_LIMITS)
    if not isinstance(limits, dict) or set(limits) != set(DEFAULT_LIMITS):
        raise CycleError('limits require max_cycles and max_source_requests')
    if any(type(v) is not int or v < 0 for v in limits.values()):
        raise CycleError('limits must be nonnegative integers')
    return dict(limits)


def _cycle_dir(output, cid):
    return output / 'cycles' / _sha256_text(cid)[:32]


def _lock_and_validate(output):
    import fcntl
    output.mkdir(parents=True, exist_ok=True)
    handle = (output / '.pipeline.lock').open('a+')
    fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


def _reservation(req, state, limits):
    need = 3 if req['source_mode'] == 'sec' else 0
    used = state['resources']
    denied = used['cycles'] + 1 > limits['max_cycles'] or used['source_requests'] + need > limits['max_source_requests']
    return {'cycles': 0 if denied else 1, 'source_requests': 0 if denied else need}, denied


def _charged(base, reservation):
    return {'cycles': base['resources']['cycles'] + reservation['cycles'],
            'source_requests': base['resources']['source_requests'] + reservation['source_requests'],
            'model_usd': '0'}


def _pause_decisions(cid, reason):
    return [{'book': b, 'symbol': s, 'timestamp': cid, 'action': 'abstain', 'reason': reason}
            for b in ('research', 'intraday') for s in UNIVERSE]


def _make_terminal(intent, base, status, state=None, decisions=None, analyses=None, bundle=None):
    state = copy.deepcopy(base if state is None else state)
    state['resources'] = _charged(base, intent['reservation'])
    body = {'cycle_id': intent['request']['cycle_id'], 'request_sha256': intent['request_sha256'],
            'previous_hash': intent['previous_hash'], 'status': status, 'state': state,
            'decisions': decisions if decisions is not None else _pause_decisions(intent['request']['cycle_id'], status),
            'analyses': analyses if analyses is not None else {}, 'source_bundle': bundle if bundle is not None else {}}
    body['receipt_hash'] = _sha256_text(_canonical(body))
    return body


def _validate_terminal(term, intent, base):
    import ledger
    import research
    required = {'cycle_id', 'request_sha256', 'previous_hash', 'status', 'state', 'decisions', 'analyses', 'source_bundle', 'receipt_hash'}
    if not isinstance(term, dict) or set(term) != required:
        raise CycleError('terminal schema mismatch')
    body = {k: v for k, v in term.items() if k != 'receipt_hash'}
    if term['receipt_hash'] != _sha256_text(_canonical(body)):
        raise CycleError('terminal hash mismatch')
    req = intent['request']
    if (term['cycle_id'], term['request_sha256'], term['previous_hash']) != (req['cycle_id'], intent['request_sha256'], intent['previous_hash']):
        raise CycleError('terminal intent binding mismatch')
    status = term['status']
    if intent['denied']:
        expected = _make_terminal(intent, base, 'budget_exhausted')
    elif status == 'interrupted':
        expected = _make_terminal(intent, base, 'interrupted')
    elif status in ('complete', 'paused_sources'):
        bundle = term['source_bundle']
        if not isinstance(bundle, dict) or set(bundle) != set(UNIVERSE):
            raise CycleError('source bundle schema mismatch')
        if req['source_mode'] == 'fixture' and bundle != req['fixture_sources']:
            raise CycleError('fixture source mismatch')
        analyses = research.analyze(bundle, base['research'], req['cycle_id'])
        state, decisions = ledger.advance(base, analyses, req['prices'], req['cycle_id'], req['session_close'])
        status = 'paused_sources' if all(a['stance'] == 'abstain' for a in analyses.values()) else 'complete'
        expected = _make_terminal(intent, base, status, state, decisions, analyses, bundle)
    else:
        raise CycleError('terminal status invalid')
    if term != expected:
        raise CycleError('terminal computation or resource mismatch')
    ledger.reconcile(term['state'])


def _load_corpus(output, limits=None):
    """Validate all intents, prepared snapshots and receipts without external I/O."""
    import contracts
    import ledger
    frozen_path = output / '.limits.json'
    frozen = _check_limits(_read_json(frozen_path)) if frozen_path.exists() else None
    if limits is not None and frozen is not None and limits != frozen:
        raise CycleError('limits are frozen for this history')
    rows = []
    for cd in (output / 'cycles').glob('*'):
        if not cd.is_dir() or not (cd / 'intent.json').exists():
            raise CycleError('orphan cycle artifact')
        intent = _read_json(cd / 'intent.json')
        keys = {'request', 'request_sha256', 'limits', 'fingerprint', 'previous_hash', 'reservation', 'denied'}
        if set(intent) != keys or frozen is None:
            raise CycleError('intent schema or missing limits')
        req = contracts.validate_request(intent['request'])
        if req != intent['request'] or cd != _cycle_dir(output, req['cycle_id']):
            raise CycleError('intent identity mismatch')
        if intent['request_sha256'] != _sha256_text(_canonical(req)) or intent['limits'] != frozen or intent['fingerprint'] != _fingerprint():
            raise CycleError('intent hash, limits or code mismatch')
        rows.append((req['cycle_id'], cd, intent))
    rows.sort(key=lambda r: r[0])
    receipts, pending = [], None
    base = ledger.initial_state()
    previous = None
    for cid, cd, intent in rows:
        if pending is not None:
            raise CycleError('history after pending intent')
        reservation, denied = _reservation(intent['request'], base, frozen)
        if intent['reservation'] != reservation or type(intent['denied']) is not bool or intent['denied'] != denied or intent['previous_hash'] != previous:
            raise CycleError('intent chain or budget reservation mismatch')
        prep = _read_json(cd / 'prepared.json') if (cd / 'prepared.json').exists() else None
        rec = _read_json(cd / 'receipt.json') if (cd / 'receipt.json').exists() else None
        if prep is not None:
            _validate_terminal(prep, intent, base)
        if rec is not None:
            _validate_terminal(rec, intent, base)
            if prep is not None and rec != prep:
                raise CycleError('prepared receipt divergence')
            receipts.append(rec)
            base, previous = rec['state'], rec['receipt_hash']
        else:
            pending = (cd, intent, prep)
    return receipts, pending, base, previous


def _render(output, history):
    import desk
    if history:
        _write_atomic(output / 'dashboard.html', desk.render(history[-1], history).encode())


def _write_rejection(output, raw, reason):
    norm = _normalize_request(raw)
    artifact = {'status': 'invalid_input', 'reason': reason, 'request': norm}
    digest = _sha256_text(_canonical(artifact))
    target = output / 'rejections' / (digest + '.json')
    if not target.exists():
        _write_json(target, artifact)
    return artifact


def _normalize_request(raw):
    # repr only for non-JSON values; preserve valid rejection inputs exactly.
    try:
        _canonical(raw)
        return raw
    except (ValueError, TypeError):
        return repr(raw)


def run_cycle(request, output, limits=None, crash=None):
    """Complete or recover one bounded research/simulation transaction."""
    import contracts
    import ledger
    import research
    import sources
    limits = _check_limits(limits)
    output = _check_output_path(output)
    if crash not in (None, 'after_intent', 'after_prepared', 'after_receipt'):
        raise CycleError('unknown crash point')
    with _lock_and_validate(output):
        try:
            history, pending, base, previous = _load_corpus(output, limits)
            try:
                req = contracts.validate_request(request)
            except (ValueError, TypeError) as exc:
                return _write_rejection(output, request, str(exc))
            cid = req['cycle_id']
            digest = _sha256_text(_canonical(req))
            for rec in history:
                if rec['cycle_id'] == cid:
                    if digest != rec['request_sha256']:
                        raise CycleError('same cycle with changed request')
                    _render(output, history)
                    return rec
            if pending:
                cd, intent, prepared = pending
                if intent['request'] != req:
                    raise CycleError('pending request changed or different cycle')
                terminal = prepared or _make_terminal(intent, base, 'budget_exhausted' if intent['denied'] else 'interrupted')
            else:
                if history and cid <= history[-1]['cycle_id']:
                    raise CycleError('cycle must be later than history')
                reservation, denied = _reservation(req, base, limits)
                intent = {'request': req, 'request_sha256': digest, 'limits': limits, 'fingerprint': _fingerprint(),
                          'previous_hash': previous, 'reservation': reservation, 'denied': denied}
                cd = _cycle_dir(output, cid)
                if not (output / '.limits.json').exists():
                    _write_json(output / '.limits.json', limits)
                _write_json(cd / 'intent.json', intent)
                if crash == 'after_intent':
                    raise Crash('after_intent')
                if denied:
                    terminal = _make_terminal(intent, base, 'budget_exhausted')
                else:
                    if req['source_mode'] == 'fixture':
                        bundle = copy.deepcopy(req['fixture_sources'])
                    else:
                        try:
                            bundle, attempted = sources.download(cid, cd / 'sources')
                            if type(attempted) is not int or not 0 <= attempted <= 3:
                                raise CycleError('source reservation exceeded')
                        except Exception as exc:
                            bundle = {s: {'status': 'failed', 'reason': f'Source failure: {type(exc).__name__}: {exc}'} for s in UNIVERSE}
                    analyses = research.analyze(bundle, base['research'], cid)
                    state, decisions = ledger.advance(base, analyses, req['prices'], cid, req['session_close'])
                    status = 'paused_sources' if all(a['stance'] == 'abstain' for a in analyses.values()) else 'complete'
                    terminal = _make_terminal(intent, base, status, state, decisions, analyses, bundle)
                _validate_terminal(terminal, intent, base)
                _write_json(cd / 'prepared.json', terminal)
                if crash == 'after_prepared':
                    raise Crash('after_prepared')
            _write_json(cd / 'receipt.json', terminal)
            if crash == 'after_receipt':
                raise Crash('after_receipt')
            _render(output, history + [terminal])
            return terminal
        except (ValueError, KeyError, TypeError) as exc:
            raise CycleError(str(exc)) from exc


def report_only(output):
    """Rebuild the latest dashboard using only verified persisted history."""
    output = _check_output_path(output)
    if not output.exists():
        raise CycleError('no history')
    with _lock_and_validate(output):
        history, pending, _, _ = _load_corpus(output)
        if not history:
            raise CycleError('no committed cycles')
        _render(output, history)
        return history[-1]


def _main(argv):
    args = argv[1:]
    if "--report-only" in args:
        try:
            term = report_only(args[args.index("--report-only") + 1])
            print(_canonical(term))
            return 0
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            return 2
    request_path = args[args.index("--request") + 1] if "--request" in args else None
    output = args[args.index("--output") + 1] if "--output" in args else None
    limits_path = args[args.index("--limits") + 1] if "--limits" in args else None
    if not request_path or not output:
        print("usage: --request FILE --output DIR [--limits FILE]", file=sys.stderr)
        return 2
    try:
        with open(request_path, "r", encoding="utf-8") as fh:
            request = json.load(fh)
        limits = None
        if limits_path:
            with open(limits_path, "r", encoding="utf-8") as fh:
                limits = json.load(fh)
        term = run_cycle(request, output, limits=limits)
        print(_canonical(term))
        return 0 if term.get("status") == "complete" else 1
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
