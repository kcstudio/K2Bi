"""Locked atomic journal with frozen inputs and deterministic whole-history replay."""
from __future__ import annotations

import copy
import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
import tempfile
import sys
from . import inputs, ledger

RECEIPT_KEYS = set('request request_sha256 previous_hash receipt_hash state decisions status'.split())


def safe_path(path):
    """Reject symlinks in every existing component, including dangling links."""
    path = Path(os.path.abspath(path))
    # macOS exposes /tmp and /var as fixed operating-system aliases.
    if sys.platform == 'darwin':
        for alias in ('/tmp', '/var', '/etc'):
            root = Path(alias)
            if (path == root or root in path.parents) and root.is_symlink() and os.readlink(root) == 'private'+alias:
                path = Path('/private'+str(path))
                break
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError(f'symlink path forbidden: {part}')
    return path


def atomic(path, obj):
    """Replace one JSON file and fsync both file and parent directory."""
    safe_path(path)
    handle, name = tempfile.mkstemp(prefix='.eod-', dir=path.parent)
    try:
        with os.fdopen(handle, 'wb') as stream:
            stream.write(inputs.canonical(obj)); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def locked(directory, *, create=False):
    """Require an exclusive nonblocking lock. Never run unlocked."""
    directory = safe_path(directory)
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    if not directory.is_dir():
        raise ValueError('state directory missing')
    path = safe_path(directory/'lock')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError('practice state is locked') from exc
        yield directory
    finally:
        os.close(fd)


def validate_request(request):
    """Validate complete exact frozen request before persisting an intent."""
    ledger.keys(request, ('as_of', 'analyses', 'bars', 'evidence'), 'request')
    if not isinstance(request['evidence'], dict):
        raise ValueError('evidence must be a mapping')
    if request['evidence'].get('origin') not in ('synthetic_fixture', 'normalized_fixture', 'saved_provider_evidence'):
        raise ValueError('evidence origin must be explicit')
    if 'companies' in request['evidence']:
        companies = request['evidence']['companies']
        if not isinstance(companies, dict) or not set(companies) <= set(ledger.SYMBOLS):
            raise ValueError('invalid company evidence mapping')
        for metadata in companies.values():
            ledger.keys(metadata, ('industry', 'source'), 'company evidence')
            ledger.text(metadata['industry'])
            if not isinstance(metadata['source'], dict):
                raise ValueError('invalid company source metadata')
    inputs.canonical(request)
    ledger.validate_inputs(request['as_of'], request['analyses'], request['bars'])
    fixtures = any(r['origin'] == 'fixture' for r in request['bars'].values())
    if fixtures and request['evidence'].get('origin') not in ('synthetic_fixture', 'normalized_fixture'):
        raise ValueError('normalized fixtures require explicit evidence disclosure')


def receipt(request, state, previous):
    """Execute the pure ledger and seal one complete cycle."""
    output = ledger.advance(state, as_of=request['as_of'], analyses=request['analyses'], bars=request['bars'])
    result = dict(request=copy.deepcopy(request), request_sha256=inputs.digest(request), previous_hash=previous,
                  state=output['state'], decisions=output['decisions'], status='complete')
    result['receipt_hash'] = inputs.digest(result)
    return result


def validate_receipt(result):
    """Check a standalone report receipt seal, inputs, state and decisions."""
    ledger.keys(result, RECEIPT_KEYS, 'receipt')
    inputs.sealed(result)
    validate_request(result['request'])
    if result['request_sha256'] != inputs.digest(result['request']):
        raise ValueError('request digest mismatch')
    if result['previous_hash'] is not None:
        ledger.sha(result['previous_hash'])
    ledger.reconcile(result['state'])
    if result['state']['last_cycle'] != ledger.iso(ledger.aware(result['request']['as_of'])):
        raise ValueError('receipt cycle mismatch')
    if not isinstance(result['decisions'], list) or len(result['decisions']) != 10:
        raise ValueError('receipt needs ten decisions')
    expected = {(b, s) for b in ('research', 'intraday') for s in ledger.SYMBOLS}
    seen = set()
    for row in result['decisions']:
        ledger.keys(row, ('book', 'symbol', 'action', 'reason', 'timestamp'), 'decision')
        ledger.text(row['book']); ledger.text(row['symbol'])
        pair = (row['book'], row['symbol'])
        if pair not in expected or pair in seen or row['action'] not in ('wait', 'queue_buy', 'buy', 'hold', 'queue_sell', 'sell'):
            raise ValueError('invalid decision')
        if row['timestamp'] != result['state']['last_cycle'] or row['book'] == 'intraday' and row['action'] != 'wait':
            raise ValueError('decision chronology/activity mismatch')
        ledger.text(row['reason']); seen.add(pair)


def history(directory):
    """Validate every journal entry against pure replay, with no skipping."""
    path, marker = safe_path(directory/'journal.json'), safe_path(directory/'initialized.json')
    if not path.exists():
        if marker.exists():
            raise ValueError('initialized journal missing')
        return [], ledger.initial_state(), None
    if not marker.exists():
        raise ValueError('journal initialization marker missing')
    initialized = inputs.decode(marker.read_bytes())
    ledger.keys(initialized, ('schema',), 'initialization marker')
    if type(initialized['schema']) is not int or initialized['schema'] != 1:
        raise ValueError('journal initialization marker changed')
    journal = inputs.decode(path.read_bytes())
    ledger.keys(journal, ('schema', 'entries', 'journal_hash'), 'journal')
    if journal['journal_hash'] != inputs.digest({k:v for k,v in journal.items() if k != 'journal_hash'}):
        raise ValueError('whole-journal integrity mismatch')
    if type(journal['schema']) is not int or journal['schema'] != 1 or not isinstance(journal['entries'], list) or not journal['entries']:
        raise ValueError('invalid journal schema/history')
    state, previous = ledger.initial_state(), None
    for index, entry in enumerate(journal['entries']):
        ledger.keys(entry, ('request', 'request_hash', 'receipt'), 'entry')
        validate_request(entry['request'])
        if entry['request_hash'] != inputs.digest(entry['request']):
            raise ValueError('corrupt frozen request')
        expected = receipt(entry['request'], state, previous)
        if entry['receipt'] is None:
            if index != len(journal['entries'])-1:
                raise ValueError('unfinished intent not last')
        else:
            if entry['receipt'] != expected:
                raise ValueError('receipt/history differs from deterministic replay')
            state, previous = expected['state'], expected['receipt_hash']
    return journal['entries'], state, previous


def persist(directory, entries):
    obj = {'schema': 1, 'entries': entries}
    obj['journal_hash'] = inputs.digest(obj)
    atomic(directory/'journal.json', obj)


def run_cycle(directory: Path, request: dict, *, fault: str | None = None):
    """Freeze exact input then commit, supporting same-input idempotency."""
    if fault not in (None, 'after_intent', 'after_receipt'):
        raise ValueError('unknown fault injection')
    validate_request(request)
    request = copy.deepcopy(request)
    with locked(directory, create=True) as directory:
        entries, state, previous = history(directory)
        for entry in entries:
            if ledger.aware(entry['request']['as_of']) == ledger.aware(request['as_of']):
                if entry['request'] != request:
                    raise ValueError('same-cycle changed-input conflict')
                if entry['receipt'] is not None:
                    return copy.deepcopy(entry['receipt'])
                result = receipt(entry['request'], state, previous)
                entry['receipt'] = result; persist(directory, entries)
                return result
        if entries and entries[-1]['receipt'] is None:
            raise ValueError('recover unfinished frozen intent first')
        result = receipt(request, state, previous)  # Validate transition before writing anything.
        if not entries:
            atomic(directory/'initialized.json', {'schema': 1})
        entries.append(dict(request=request, request_hash=inputs.digest(request), receipt=None))
        persist(directory, entries)
        if fault == 'after_intent':
            raise RuntimeError('local injected crash after intent')
        entries[-1]['receipt'] = result; persist(directory, entries)
        if fault == 'after_receipt':
            raise RuntimeError('local injected crash after receipt')
        return copy.deepcopy(result)


def recover(directory: Path):
    """Commit only the exact durable unfinished input, without replacement."""
    with locked(directory) as directory:
        entries, state, previous = history(directory)
        if not entries:
            return None
        if entries[-1]['receipt'] is None:
            entries[-1]['receipt'] = receipt(entries[-1]['request'], state, previous)
            persist(directory, entries)
        return copy.deepcopy(entries[-1]['receipt'])


def latest(directory: Path):
    """Read verified committed history without advancing or rewriting JSON."""
    with locked(directory) as directory:
        entries, _, _ = history(directory)
        complete = [e['receipt'] for e in entries if e['receipt'] is not None]
        return copy.deepcopy(complete[-1]) if complete else None
