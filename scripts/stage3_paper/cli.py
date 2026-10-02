"""Stage 3 paper snapshot CLI, read only."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from scripts.stage2_eod import cli as _eod_cli
from scripts.stage2_eod import store as _store
from scripts.stage3_paper import desk as _desk


def _parser():
    parser = argparse.ArgumentParser(prog="scripts.stage3_paper.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    render = sub.add_parser("render")
    render.add_argument("--state-dir", required=True)
    render.add_argument("--snapshot", default=None)
    render.add_argument("--as-of", required=True)
    render.add_argument("--output", required=True)
    return parser


def render_command(args, *, out_write=None):
    state_dir = Path(args.state_dir)
    output = Path(args.output)
    if output.suffix.lower() != ".html":
        sys.stderr.write("output must end with .html\n")
        return 2
    try:
        receipt = _store.latest(state_dir)
    except Exception:
        sys.stderr.write("failed to read committed receipt\n")
        return 2
    if receipt is None:
        sys.stderr.write("no committed receipt available\n")
        return 2
    sources = []
    snapshot_raw = None
    if args.snapshot:
        try:
            safe_snapshot = _store.safe_path(Path(args.snapshot))
        except Exception:
            sys.stderr.write("refused snapshot path\n")
            return 2
        if not safe_snapshot.is_file() or safe_snapshot.is_symlink():
            sys.stderr.write("snapshot path must be a regular resolved file\n")
            return 2
        try:
            snapshot_raw = safe_snapshot.read_bytes()
        except OSError:
            sys.stderr.write("failed to read snapshot\n")
            return 2
        sources.append(safe_snapshot)
    try:
        safe_state, safe_output = _eod_cli.destinations(state_dir, output, sources=tuple(sources))
    except Exception:
        sys.stderr.write("refused output destination\n")
        return 2
    try:
        page = _desk.render(receipt, snapshot_raw, as_of=args.as_of)
    except ValueError as exc:
        sys.stderr.write("snapshot failed closed: " + str(exc) + "\n")
        return 2
    writer = out_write or _eod_cli.publish
    try:
        writer(safe_output, page)
    except Exception:
        sys.stderr.write("failed to write output\n")
        return 2
    return 0


def main(argv=None, *, out_write=None):
    args = _parser().parse_args(argv)
    if args.command == "render":
        return render_command(args, out_write=out_write)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
