"""Canonical ticker registry -- one-time build from NASDAQ + NYSE listed equities.

Refresh quarterly or when unknown_ticker rate exceeds 5% over 10 runs.
"""

from __future__ import annotations

import csv
import json
import urllib.request

import yaml
from datetime import datetime, timezone
from pathlib import Path

from scripts.lib.invest_ship_strategy import resolve_vault_root

NASDAQ_API_URL = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit={limit}&offset={offset}&exchange={exchange}"

# Page size for NASDAQ API pagination
_PAGE_SIZE = 100


def _registry_path(vault_root: Path | None = None) -> Path:
    root = resolve_vault_root(vault_root)
    return root / "wiki" / "tickers" / "canonical-registry.json"


def load_registry(vault_root: Path | None = None) -> dict[str, dict]:
    """Load the canonical registry as {symbol: {exchange, name, last_updated_utc}}.

    Returns an empty dict when the registry file does not exist.
    """
    path = _registry_path(vault_root)
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _fetch_exchange(exchange: str) -> list[dict]:
    """Paginate through the NASDAQ screener API for one exchange.

    Returns a list of row dicts with at least ``symbol`` and ``name`` keys.
    """
    rows: list[dict] = []
    offset = 0
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    while True:
        url = NASDAQ_API_URL.format(limit=_PAGE_SIZE, offset=offset, exchange=exchange)
        req = urllib.request.Request(url, method="GET", headers=headers)
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        page_rows = data.get("data", {}).get("table", {}).get("rows", [])
        if not page_rows:
            break
        rows.extend(page_rows)
        offset += _PAGE_SIZE
        total_raw = data.get("data", {}).get("totalrecords")
        try:
            total = int(total_raw) if total_raw is not None else None
        except (TypeError, ValueError):
            total = None
        if total is not None and offset >= total:
            break
    return rows


def _parse_api_rows(rows: list[dict], exchange: str) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for row in rows:
        symbol = str(row.get("symbol", "")).strip()
        name = str(row.get("name", "")).strip()
        if not symbol:
            continue
        result[symbol] = {"exchange": exchange, "name": name}
    return result


def refresh_registry(vault_root: Path | None = None) -> dict[str, dict]:
    """Download NASDAQ + NYSE listings, merge, and atomic-write JSON.

    Returns the registry dict. Writes to
    ``{vault}/wiki/tickers/canonical-registry.json`` via tempfile +
    os.replace.
    """
    nasdaq_rows = _fetch_exchange("NASDAQ")
    nyse_rows = _fetch_exchange("NYSE")

    registry: dict[str, dict] = {}
    registry.update(_parse_api_rows(nasdaq_rows, "NASDAQ"))
    registry.update(_parse_api_rows(nyse_rows, "NYSE"))

    now = datetime.now(timezone.utc).isoformat()
    for info in registry.values():
        info["last_updated_utc"] = now

    path = _registry_path(vault_root)
    # Import here to avoid circular import at module load time
    from scripts.lib.strategy_frontmatter import atomic_write_bytes

    payload = json.dumps(registry, indent=2, sort_keys=True).encode("utf-8")
    atomic_write_bytes(path, payload)

    # Ensure tickers index exists
    _ensure_tickers_index(path.parent, len(registry))
    return registry


def _ensure_tickers_index(tickers_dir: Path, count: int) -> None:
    from scripts.lib.strategy_frontmatter import atomic_write_bytes

    index_path = tickers_dir / "index.md"
    if index_path.exists():
        return
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    frontmatter = {
        "tags": ["tickers", "index", "k2bi"],
        "date": today,
        "type": "index",
        "origin": "k2bi-generate",
        "up": "[[index]]",
    }
    fm_lines = ["---"]
    fm_lines.extend(yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).splitlines())
    fm_lines.append("---")
    body = "\n".join(fm_lines) + "\n\n# Tickers Index\n\n"
    body += "Canonical ticker registry: [[canonical-registry.json]]\n\n"
    body += "Refresh procedure: run `python3 -m scripts.build_canonical_registry` from the K2Bi repo root. "
    body += "Refresh quarterly or when unknown_ticker rate exceeds 5% over 10 runs.\n"
    atomic_write_bytes(index_path, body.encode("utf-8"))
