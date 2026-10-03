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
    render.add_argument("--prices", default=None)
    render.add_argument("--price-proof", default=None)
    render.add_argument("--cash-snapshot", default=None)
    render.add_argument("--account-readiness", default=None)
    render.add_argument("--account-proof", default=None)
    render.add_argument("--research-evidence", default=None)
    render.add_argument("--as-of", required=True)
    render.add_argument("--output", required=True)
    return parser


def _read_source(path, label, sources):
    try:
        safe = _store.safe_path(Path(path))
    except Exception:
        sys.stderr.write("refused " + label + " path\n")
        return None
    if not safe.is_file() or safe.is_symlink():
        sys.stderr.write(label + " path must be a regular resolved file\n")
        return None
    try:
        raw = safe.read_bytes()
    except OSError:
        sys.stderr.write("failed to read " + label + "\n")
        return None
    sources.append(safe)
    return raw


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
        snapshot_raw = _read_source(args.snapshot, "snapshot", sources)
        if snapshot_raw is None:
            return 2
    prices_raw = None
    if args.prices:
        prices_raw = _read_source(args.prices, "prices", sources)
        if prices_raw is None:
            return 2
    proof_raw = _read_source(args.price_proof, "price proof", sources) if args.price_proof else None
    if args.price_proof and proof_raw is None: return 2
    config_path = _store.safe_path(Path(__file__).resolve().parents[2]/"execution/validators/config.yaml")
    config_raw = _read_source(config_path, "risk config", sources)
    if config_raw is None: return 2
    cash_raw = None
    if args.cash_snapshot:
        cash_raw = _read_source(args.cash_snapshot, "cash snapshot", sources)
        if cash_raw is None:
            return 2
    account_raw = _read_source(args.account_readiness, "account readiness", sources) if args.account_readiness else None
    account_proof_raw = _read_source(args.account_proof, "account proof", sources) if args.account_proof else None
    if (args.account_readiness and account_raw is None) or (args.account_proof and account_proof_raw is None): return 2
    research_path = getattr(args, "research_evidence", None)
    research_raw = _read_source(research_path, "research evidence", sources) if research_path else None
    if research_path and research_raw is None: return 2
    try:
        safe_state, safe_output = _eod_cli.destinations(state_dir, output, sources=tuple(sources))
    except Exception:
        sys.stderr.write("refused output destination\n")
        return 2
    try:
        page = _desk.render(receipt, snapshot_raw, as_of=args.as_of,
                            prices_raw=prices_raw, cash_raw=cash_raw, proof_raw=proof_raw, config_raw=config_raw, account_raw=account_raw, account_proof_raw=account_proof_raw, research_raw=research_raw)
    except ValueError as exc:
        sys.stderr.write("source failed closed: " + str(exc) + "\n")
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
