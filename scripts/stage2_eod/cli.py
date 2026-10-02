"""Local saved-evidence run and read-only dashboard rebuild commands."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import tempfile
from . import desk, inputs, store

ROOT = Path(__file__).resolve().parents[2]
PROTECTED = tuple(ROOT/p for p in ('scripts', 'execution', 'tests', 'wiki', '.git', '.githooks', '.agents', '.claude',
    'proposals/dashboard-ux-2026-09-26', 'proposals/stage2-ibkr-price-check-2026-09-26',
    'proposals/stage2-quotes-2026-09-26', 'proposals/research-pilot-2026-09-25')) + (ROOT.parent/'K2Bi-Vault',)


def contained(path, parent):
    return path == parent or parent in path.parents


def destinations(state, output, sources=()):
    """Protect sealed sources, products, vault and state from output collisions."""
    state, output = store.safe_path(state), store.safe_path(output)
    if output.suffix.lower() != '.html':
        raise ValueError('output must be an HTML report')
    for path in (state, output):
        if any(contained(path, p) for p in PROTECTED):
            raise ValueError('destination inside protected product/source/vault')
    if contained(output, state) or contained(state, output):
        raise ValueError('output must be separate from state')
    for source in sources:
        if output == source or contained(source, state) or contained(state, source):
            raise ValueError('destination overlaps source evidence')
    return state, output


def publish(path, page):
    """Publish a complete offline report atomically."""
    store.safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.eod-page-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(page); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main(argv=None):
    """Run offline CLI, reporting failures as exit code 2."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run')
    run.add_argument('--research', type=Path, required=True)
    run.add_argument('--prices', type=Path, required=True)
    run.add_argument('--as-of', required=True)
    rebuild = commands.add_parser('rebuild')
    for command in (run, rebuild):
        command.add_argument('--state-dir', type=Path, required=True)
        command.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        sources = tuple(store.safe_path(p) for p in (args.research, args.prices)) if args.command == 'run' else ()
        state, output = destinations(args.state_dir, args.output, sources)
        if args.command == 'run':
            request = inputs.from_saved_evidence(sources[0].read_bytes(), sources[1].read_bytes(), as_of=args.as_of)
            result = store.run_cycle(state, request)
        else:
            result = store.latest(state)
            if result is None:
                raise ValueError('no committed receipt to rebuild')
        publish(output, desk.render(result))
        print(f"Complete local practice receipt {result['receipt_hash']}; dashboard {output}")
        return 0
    except (ValueError, OSError) as exc:
        print(f'Local practice failed: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
