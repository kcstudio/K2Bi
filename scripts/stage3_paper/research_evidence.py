"""Offline research evidence parser (stdlib only, no network/filesystem).

Parses a single locally supplied research-evidence brief and returns a
normalized, display-only representation. Never authenticates content,
never unlocks a trading card, never decides funding/risk/quote/mandate.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

MAX_RAW = 1024 * 1024
MAX_SOURCES = 20
MAX_EXPIRY = timedelta(days=7)

_ROOT_KEYS = {
    "schema",
    "kind",
    "origin",
    "captured_at",
    "research_as_of",
    "expires_at",
    "symbol",
    "summary",
    "sources",
    "proposal",
}
_SOURCE_KEYS = {"uri", "published_at", "sha256"}
_PROPOSAL_KEYS = {"side", "rationale"}
_KINDS = {"offline-research-evidence"}
_ORIGINS = {"recorded", "fixture"}
_SIDES = {"buy", "sell", "watch"}

_TS_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})"
    r"(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_SYMBOL_RE = re.compile(r"^[A-Z]{1,6}$")
_CTRL_RE = re.compile(r"[\u0000-\u001f\u007f]")
_URI_RE = re.compile(r"^https://([^/?#\s]+)(/[^#\s]*)?(\?[^#\s]*)?$")


def _fail(msg):
    return ValueError(msg)


def _parse_timestamp(value, field):
    if type(value) is not str:
        raise _fail(f"{field}: must be a string")
    m = _TS_RE.fullmatch(value)
    if m is None:
        raise _fail(f"{field}: invalid timestamp")
    tz = m.groups()[-1]
    if tz != 'Z' and (int(tz[1:3]) > 23 or int(tz[4:6]) > 59):
        raise _fail(f"{field}: invalid offset")
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise _fail(f"{field}: invalid timestamp") from None


def _check_text(value, field, lo, hi):
    if type(value) is not str:
        raise _fail(f"{field}: must be a string")
    if _CTRL_RE.search(value):
        raise _fail(f"{field}: control character")
    if not value.strip():
        raise _fail(f"{field}: empty")
    if not (lo <= len(value) <= hi):
        raise _fail(f"{field}: length out of range")
    return value


def _no_dupes(pairs):
    seen = set()
    for key, _ in pairs:
        if key in seen:
            raise _fail(f"duplicate key: {key}")
        seen.add(key)
    return dict(pairs)


def _nonfinite(constant):
    raise _fail(f"nonfinite constant: {constant}")


def _check_uri(value):
    if type(value) is not str:
        raise _fail("uri: must be a string")
    if len(value) > 2048:
        raise _fail("uri: too long")
    if _CTRL_RE.search(value):
        raise _fail("uri: control character")
    m = _URI_RE.fullmatch(value)
    if m is None:
        raise _fail("uri: must be https URL without fragment")
    try:
        parts = urlsplit(value)
        if not parts.hostname or parts.username is not None or parts.password is not None:
            raise ValueError('invalid authority')
        parts.port
    except ValueError:
        raise _fail('uri: invalid HTTPS authority') from None
    authority = m.group(1)
    if "@" in authority:
        raise _fail("uri: credentials not allowed")
    host = authority.split(":", 1)[0]
    if not host:
        raise _fail("uri: empty hostname")
    return value


def _parse_source(raw, index):
    field = f"sources[{index}]"
    if type(raw) is not dict:
        raise _fail(f"{field}: must be a mapping")
    if set(raw) != _SOURCE_KEYS:
        raise _fail(f"{field}: unexpected keys")
    uri = _check_uri(raw["uri"])
    pub_raw = raw["published_at"]
    pub = _parse_timestamp(pub_raw, f"{field}.published_at")
    sha = raw["sha256"]
    if type(sha) is not str or _SHA_RE.fullmatch(sha) is None:
        raise _fail(f"{field}.sha256: invalid")
    return {"uri": uri, "published_at": pub_raw, "_pub": pub, "sha256": sha}


def _parse_proposal(raw):
    if raw is None:
        return None
    if type(raw) is not dict:
        raise _fail("proposal: must be a mapping or null")
    if set(raw) != _PROPOSAL_KEYS:
        raise _fail("proposal: unexpected keys")
    side = raw["side"]
    if type(side) is not str or side not in _SIDES:
        raise _fail("proposal.side: invalid")
    rationale = _check_text(raw["rationale"], "proposal.rationale", 1, 2000)
    return {"side": side, "rationale": rationale}


def parse_research_evidence(raw, *, as_of):
    """Parse a research-evidence brief. Pure, local, display-only."""
    if type(raw) is not bytes:
        raise _fail("raw: must be bytes")
    if len(raw) > MAX_RAW:
        raise _fail("raw: too large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _fail("raw: invalid UTF-8") from None
    try:
        doc = json.loads(
            text, object_pairs_hook=_no_dupes, parse_constant=_nonfinite
        )
    except ValueError as exc:
        if str(exc).startswith("nonfinite constant"):
            raise
        if str(exc).startswith("duplicate key"):
            raise
        raise _fail("raw: invalid JSON") from None
    if type(doc) is not dict:
        raise _fail("root: must be a mapping")
    if set(doc) != _ROOT_KEYS:
        raise _fail("root: unexpected or missing keys")

    schema = doc["schema"]
    if type(schema) is not int or schema != 1:
        raise _fail("schema: must be int")
    kind = doc["kind"]
    if type(kind) is not str or kind not in _KINDS:
        raise _fail("kind: invalid")
    origin = doc["origin"]
    if type(origin) is not str or origin not in _ORIGINS:
        raise _fail("origin: invalid")

    captured_raw = doc["captured_at"]
    research_raw = doc["research_as_of"]
    expires_raw = doc["expires_at"]
    captured = _parse_timestamp(captured_raw, "captured_at")
    research = _parse_timestamp(research_raw, "research_as_of")
    expires = _parse_timestamp(expires_raw, "expires_at")
    evaluated = _parse_timestamp(as_of, "as_of")

    if captured > evaluated:
        raise _fail("captured_at after evaluation")
    if research > captured:
        raise _fail("research_as_of after captured_at")
    if not (captured < expires):
        raise _fail("expires_at must be after captured_at")
    if expires - captured > MAX_EXPIRY:
        raise _fail("expires_at exceeds 7 days from captured_at")

    symbol = doc["symbol"]
    if type(symbol) is not str or _SYMBOL_RE.fullmatch(symbol) is None:
        raise _fail("symbol: invalid")
    summary = _check_text(doc["summary"], "summary", 1, 2000)

    sources_raw = doc["sources"]
    if type(sources_raw) is not list:
        raise _fail("sources: must be a list")
    if not (1 <= len(sources_raw) <= MAX_SOURCES):
        raise _fail("sources: count out of range")

    sources = []
    seen_uris = set()
    for i, item in enumerate(sources_raw):
        src = _parse_source(item, i)
        if src["_pub"] > research:
            raise _fail(f"sources[{i}]: published after research_as_of")
        if src["uri"] in seen_uris:
            raise _fail("sources: duplicate uri")
        seen_uris.add(src["uri"])
        sources.append(src)

    proposal = _parse_proposal(doc["proposal"])

    freshness = "fresh" if evaluated < expires else "stale"
    raw_sha = hashlib.sha256(raw).hexdigest()
    provenance = (
        "supplied_unverified" if origin == "recorded" else "invented_fixture"
    )

    return {
        "schema": schema,
        "kind": kind,
        "origin": origin,
        "captured_at": captured_raw,
        "research_as_of": research_raw,
        "expires_at": expires_raw,
        "symbol": symbol,
        "summary": summary,
        "sources": [
            {
                "uri": s["uri"],
                "published_at": s["published_at"],
                "sha256": s["sha256"],
            }
            for s in sources
        ],
        "proposal": proposal,
        "evaluated_at": as_of,
        "freshness": freshness,
        "raw_sha256": raw_sha,
        "provenance": provenance,
        "recommendation_verified": False,
        "executable": False,
        "fill_eligible": False,
    }
