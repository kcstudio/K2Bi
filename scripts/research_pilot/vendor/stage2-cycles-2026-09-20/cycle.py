import argparse
import datetime as _dt
import fcntl
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

SEC_URL = "https://data.sec.gov/submissions/CIK0000813672.json"
MAX_BYTES = 2 * 1024 * 1024
TIMEOUT = 20
EXPECTED_CIK = "0000813672"
EXPECTED_TICKER = "CDNS"
IDENTITY_VERSION = "stage2-cycle-v2"
KEEP_FORMS = None


class CycleError(Exception):
    pass


class Crash(Exception):
    pass


def actual_now():
    return _dt.datetime.now(_dt.timezone.utc)


def default_fetch(url=SEC_URL, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": "K2Bi-stage2/1.0 contact@example.com"})
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            raise CycleError("redirect not permitted")
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(req, timeout=timeout) as r:
        if getattr(r, "status", 200) != 200:
            raise CycleError("http status %r" % getattr(r, "status", None))
        return r.read(MAX_BYTES + 1)


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def _write_atomic(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dfd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _slot(now):
    if now.tzinfo is None or now.utcoffset() is None:
        raise CycleError("naive clock not allowed")
    return int(now.timestamp()) // 3600


def _parse_iso_date(s):
    if not isinstance(s, str):
        raise CycleError("date not string")
    try:
        d = _dt.date.fromisoformat(s)
    except ValueError:
        raise CycleError("bad date %r" % (s,))
    if d.isoformat() != s:
        raise CycleError("non-canonical date %r" % (s,))
    return d


def _identity(source_mode, fixture_bytes, manifest_map):
    return {
        "version": IDENTITY_VERSION,
        "source": source_mode,
        "securl": SEC_URL,
        "fixture_sha256": _sha(fixture_bytes) if fixture_bytes is not None else None,
        "manifest": {str(k): str(v) for k, v in sorted((manifest_map or {}).items())},
        "hourseconds": 3600,
    }


def _load_manifest(manifest):
    if manifest is None:
        manifest = Path(__file__).with_name("frozen-stage1.json")
    p = Path(manifest)
    try:
        raw = p.read_bytes()
    except OSError as e:
        raise CycleError("manifest unreadable: %s" % e)
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise CycleError("manifest invalid: %s" % e)
    if not isinstance(data, dict) or not data:
        raise CycleError("manifest must be nonempty object")
    out = {}
    for k, v in data.items():
        if not isinstance(k, str) or not isinstance(v, str) or len(v) != 64:
            raise CycleError("bad manifest entry")
        out[k] = v
    return out


def _read_source_doc(path):
    p = Path(path)
    if not p.is_file():
        raise CycleError("missing source doc %s" % p)
    return p.read_bytes()


def _copy_frozen(output_sim, manifest_map):
    output_sim = Path(output_sim)
    output_sim.mkdir(parents=True, exist_ok=True)
    copied = []
    for src, want in sorted(manifest_map.items()):
        sp = Path(src)
        if not sp.is_file():
            raise CycleError("frozen source missing %s" % sp)
        raw = sp.read_bytes()
        got = _sha(raw)
        if got != want:
            raise CycleError("frozen hash mismatch for %s" % sp)
        dest = output_sim / sp.name
        if dest.exists():
            if dest.is_file() and dest.read_bytes() == raw:
                copied.append(dest.name)
                continue
            raise CycleError("existing frozen target differs: %s" % dest)
        _write_atomic(dest, raw)
        copied.append(dest.name)
    return copied


def _extract_filings(doc, limit=10):
    if not isinstance(doc, dict):
        raise CycleError("root doc not dict")
    cik = doc.get("cik")
    if isinstance(cik, bool):
        raise CycleError("bad cik %r" % (cik,))
    if isinstance(cik, int):
        cik = str(cik)
    if not isinstance(cik, str) or not cik.isdigit() or len(cik) > 10:
        raise CycleError("bad cik %r" % (cik,))
    cik = cik.zfill(10)
    if cik != "0000813672":
        raise CycleError("wrong cik %r" % (cik,))
    tickers = doc.get("tickers")
    if not isinstance(tickers, list) or EXPECTED_TICKER not in tickers:
        raise CycleError("missing CDNS ticker")
    name = doc.get("name")
    if not isinstance(name, str) or "CADENCE" not in name.upper():
        raise CycleError("wrong issuer name %r" % (name,))
    filings_doc = doc.get("filings")
    if not isinstance(filings_doc, dict):
        raise CycleError("filings not dict")
    recent = filings_doc.get("recent")
    if not isinstance(recent, dict):
        raise CycleError("missing filings.recent")
    cols = ["accessionNumber", "filingDate", "form", "primaryDocument"]
    arrays = {}
    for c in cols:
        v = recent.get(c)
        if not isinstance(v, list):
            raise CycleError("missing column %s" % c)
        arrays[c] = v
    n = len(arrays["accessionNumber"])
    for c in cols[1:]:
        if len(arrays[c]) != n:
            raise CycleError("column length mismatch %s" % c)
    if n == 0:
        raise CycleError("no filings")
    out = []
    for i in range(n):
        if len(out) >= limit:
            break
        acc = arrays["accessionNumber"][i]
        fdate = arrays["filingDate"][i]
        form = arrays["form"][i]
        pdoc = arrays["primaryDocument"][i]
        if not isinstance(acc, str) or not acc:
            raise CycleError("bad accession")
        if not isinstance(form, str) or not form:
            raise CycleError("bad form")
        if not isinstance(pdoc, str) or not pdoc:
            raise CycleError("bad primaryDocument")
        d = _parse_iso_date(fdate)
        out.append({"accessionNumber": acc, "filingDate": d.isoformat(), "form": form, "primaryDocument": pdoc})
    return {"cik": cik, "name": name, "tickers": list(tickers), "filings": out}


def _validate_filings_against_now(filings, now):
    from zoneinfo import ZoneInfo
    ny_date = now.astimezone(ZoneInfo("America/New_York")).date() if now.utcoffset() is not None else None
    if ny_date is None:
        raise CycleError("naive clock")
    for f in filings:
        d = _parse_iso_date(f["filingDate"])
        if d > ny_date:
            raise CycleError("filing date in future %s" % f["filingDate"])


def _read_intent(path):
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise CycleError("cannot read intent: %s" % e)
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise CycleError("corrupt intent: %s" % e)
    return data


def _read_receipt(path):
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise CycleError("cannot read receipt: %s" % e)
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise CycleError("corrupt receipt: %s" % e)
    return data, raw


def _enumerate_slots(cycles):
    slots = []
    if not Path(cycles).is_dir():
        return slots
    for entry in os.listdir(cycles):
        if entry.isdigit():
            p = Path(cycles) / entry
            if p.is_dir():
                slots.append(int(entry))
    return sorted(slots)


def _validate_receipt_shape(r, slot):
    if not isinstance(r, dict):
        raise CycleError("receipt not object")
    if r.get("slot") != slot:
        raise CycleError("receipt slot mismatch")
    mode = r.get("mode")
    if mode not in ("success", "failed", "interrupted"):
        raise CycleError("bad receipt mode %r" % (mode,))
    if "identity" not in r or not isinstance(r["identity"], dict):
        raise CycleError("receipt identity missing")
    if "source" not in r or not isinstance(r["source"], dict):
        raise CycleError("receipt source missing")
    if mode == "success":
        s = r["source"]
        for k in ("sha256", "size", "retrieved_at", "filings"):
            if k not in s:
                raise CycleError("receipt source missing %s" % k)
        if not isinstance(s["sha256"], str) or len(s["sha256"]) != 64:
            raise CycleError("bad capture hash")
        if not isinstance(s["size"], int) or s["size"] < 0:
            raise CycleError("bad capture size")
        if not isinstance(s["filings"], list):
            raise CycleError("bad filings")
        _parse_iso_date(s["retrieved_at"][:10])


def _validate_intent(i, slot):
    if not isinstance(i, dict):
        raise CycleError("intent not object")
    if i.get("slot") != slot:
        raise CycleError("intent slot mismatch")
    if not isinstance(i.get("identity"), dict):
        raise CycleError("intent identity missing")
    if "fingerprint" not in i:
        raise CycleError("intent fingerprint missing")
    if i["fingerprint"] != _fingerprint(i["identity"]):
        raise CycleError("intent fingerprint mismatch")


def _fingerprint(identity):
    blob = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha(blob)


def _render_html(out, cycles_dir, sim_dir, last):
    esc = lambda s: (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'><title>Stage 2 cycle</title>",
        "<style>body{font-family:sans-serif;margin:1.5rem;max-width:60rem}table{border-collapse:collapse}td,th{border:1px solid #999;padding:.25rem .5rem;text-align:left}.warn{background:#ffe8e8;padding:.5rem;border:1px solid #c66}.meta{color:#555;font-size:.9rem}</style>",
        "</head><body><h1>Stage 2 cycle status</h1>",
        "<p class='warn'>Derived report. Receipts and source captures are the evidence. Not current trade advice. Synthetic Stage 1 replay only; not capital continuity.</p>",
        "<p class='meta'>Simulation copies: %s</p>" % esc(", ".join(sorted(p.name for p in Path(sim_dir).glob("*"))) if Path(sim_dir).is_dir() else "none"),
    ]
    if (Path(sim_dir) / "dashboard.html").is_file():
        parts.append("<p><a href='simulation/dashboard.html'>Simulation dashboard</a></p>")
    else:
        parts.append("<p class='warn'>Simulation dashboard unavailable.</p>")
    if last is None:
        parts.append("<p>No cycle recorded yet.</p>")
    else:
        slot, receipt = last
        parts.append("<h2>Latest slot %d</h2>" % slot)
        parts.append("<p>Mode: <b>%s</b></p>" % esc(receipt.get("mode")))
        parts.append("<p>Identity fingerprint: <code>%s</code></p>" % esc(_fingerprint(receipt["identity"])))
        identity = receipt.get("identity") or {}
        src_label = identity.get("source")
        parts.append("<p>Source: <b>%s</b></p>" % esc(src_label if src_label in ("fixture", "live", "manual") else "unknown"))
        src = receipt.get("source", {})
        if receipt.get("mode") == "success":
            parts.append("<p>Capture sha256=<code>%s</code> size=%s retrieved_at=%s</p>" % (
                esc(src.get("sha256")), esc(src.get("size")), esc(src.get("retrieved_at"))))
            parts.append("<table><tr><th>Date</th><th>Form</th><th>Accession</th><th>Document</th></tr>")
            for f in src.get("filings", []):
                parts.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                    esc(f.get("filingDate")), esc(f.get("form")),
                    esc(f.get("accessionNumber")), esc(f.get("primaryDocument"))))
            parts.append("</table>")
        else:
            parts.append("<p>Failure: %s</p>" % esc(src.get("error", "unknown")))
        parts.append("<h3>Stage 1 portfolio (synthetic, unchanged)</h3>")
        parts.append("<p>Historical synthetic replay reused; separate portfolios; no aggregation of profit.</p>")
    parts.append("</body></html>")
    _write_atomic(Path(out) / "dashboard.html", "".join(parts).encode("utf-8"))


def _recover_pending(cycles_dir, slots, manifest_map):
    pending = []
    for slot in slots:
        i, r, raw = _read_slot(cycles_dir, slot)
        if i is None:
            raise CycleError("slot %d missing intent" % slot)
        _validate_intent(i, slot)
        if r is not None:
            _validate_receipt_shape(r, slot)
            if r["mode"] == "success":
                _verify_capture(cycles_dir, slot, r)
            continue
        pending.append((slot, i))
    for slot, i in pending:
        rpath = Path(cycles_dir) / str(slot) / "receipt.json"
        receipt = {
            "slot": slot,
            "mode": "interrupted",
            "identity": i["identity"],
            "source": {"error": "interrupted before terminal commit"},
        }
        _write_atomic(rpath, json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _read_slot(cycles_dir, slot):
    d = Path(cycles_dir) / str(slot)
    if not d.exists():
        return None, None, None
    i = _read_intent(d / "intent.json")
    if i is None:
        raise CycleError("missing intent in slot %s" % slot)
    _validate_intent(i, slot)
    r = _read_receipt(d / "receipt.json")
    if r is None:
        return i, None, None
    _validate_receipt_shape(r[0], slot)
    if r[0].get("identity") != i["identity"]:
        raise CycleError("receipt identity mismatch")
    return i, r[0], r[1]


def _verify_capture(cycles_dir, slot, receipt):
    if receipt.get("mode") != "success":
        return
    p = Path(cycles_dir) / str(slot) / "source.json"
    if not p.is_file():
        raise CycleError("missing source capture for success receipt")
    raw = p.read_bytes()
    s = receipt["source"]
    if _sha(raw) != s["sha256"] or len(raw) != s["size"]:
        raise CycleError("source capture does not match receipt")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise CycleError("capture not JSON: %s" % e)
    recs = _extract_filings(doc)
    if recs["filings"] != s["filings"]:
        raise CycleError("recomputed filings differ from receipt")
    if recs["cik"] != EXPECTED_CIK or EXPECTED_TICKER not in recs["tickers"]:
        raise CycleError("capture identity mismatch")


def APIcycle(output, *, now=None, fixture=None, refresh=False, fetch=None, crash=None, manifest=None, report_only=False):
    output = Path(output)
    if not output.is_absolute():
        output = Path.cwd() / output
    manifest_map = _load_manifest(manifest)
    cycles_dir = output / "cycles"
    sim_dir = output / "simulation"
    output.mkdir(parents=True, exist_ok=True)
    lock_path = output / ".lock"
    lf = open(lock_path, "ab")
    try:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)

        if refresh and (now is not None or fetch is not None or fixture is not None):
            raise CycleError("refresh cannot combine with now/fetch/fixture")
        if refresh and report_only:
            raise CycleError("refresh cannot combine with report-only")
        if report_only:
            slots = _enumerate_slots(cycles_dir)
            if not slots:
                raise CycleError("report-only requires prior slot")
            last_slot = slots[-1]
            i, r, _ = _read_slot(cycles_dir, last_slot)
            if i is None or r is None:
                raise CycleError("report-only: slot incomplete")
            if r["mode"] == "success":
                _verify_capture(cycles_dir, last_slot, r)
            _copy_frozen(sim_dir, manifest_map)
            _render_html(output, cycles_dir, sim_dir, (last_slot, r))
            return {"slot": last_slot, "mode": r["mode"], "reused": True}
        if refresh:
            eff_now = actual_now()
            source_mode = "live"
            fixture_bytes = None
        else:
            if now is None:
                raise CycleError("non-live cycle requires now or fixture")
            if now.tzinfo is None or now.utcoffset() is None:
                raise CycleError("naive clock not allowed")
            eff_now = now
            if fixture is not None:
                fb = Path(fixture)
                if not fb.is_file():
                    raise CycleError("fixture missing %s" % fb)
                fixture_bytes = fb.read_bytes()
                source_mode = "fixture"
            else:
                fixture_bytes = None
                source_mode = "manual"

        slot = _slot(eff_now)
        identity = _identity(source_mode, fixture_bytes, manifest_map)
        fp = _fingerprint(identity)

        slots = _enumerate_slots(cycles_dir)

        if slots and slot < slots[-1]:
            raise CycleError("clock rollback below slot %d" % slots[-1])

        existing_intent, existing_receipt, existing_raw = _read_slot(cycles_dir, slot)
        if existing_intent is not None and existing_intent.get("fingerprint") != fp:
            raise CycleError("identity mismatch for existing slot intent")
        _recover_pending(cycles_dir, slots, manifest_map)
        existing_intent, existing_receipt, existing_raw = _read_slot(cycles_dir, slot)
        if existing_intent is not None:
            if existing_receipt["identity"] != identity:
                raise CycleError("receipt identity mismatch")
            if existing_receipt["mode"] == "success":
                _verify_capture(cycles_dir, slot, existing_receipt)
            if crash == "before_receipt_report":
                raise Crash("crash before report rebuild")
            _copy_frozen(sim_dir, manifest_map)
            _render_html(output, cycles_dir, sim_dir, (slot, existing_receipt))
            return {"slot": slot, "mode": existing_receipt["mode"], "reused": True}

        # New cycle for this slot.
        slot_dir = cycles_dir / str(slot)
        slot_dir.mkdir(parents=True, exist_ok=True)
        intent = {
            "slot": slot,
            "identity": identity,
            "fingerprint": fp,
            "created_at": eff_now.isoformat(),
        }
        _write_atomic(slot_dir / "intent.json", json.dumps(intent, sort_keys=True, separators=(",", ":")).encode("utf-8"))

        if crash == "after_intent":
            raise Crash("injected crash after intent")

        def _fail(reason):
            receipt = {"slot": slot, "mode": "failed", "identity": identity,
                       "source": {"error": str(reason)}}
            _write_atomic(slot_dir / "receipt.json",
                          json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8"))
            _copy_frozen(sim_dir, manifest_map)
            _render_html(output, cycles_dir, sim_dir, (slot, receipt))
            return {"slot": slot, "mode": "failed", "reused": False}

        try:
            if source_mode == "live":
                raw = (fetch or default_fetch)(SEC_URL, timeout=TIMEOUT) if fetch is not None else default_fetch(SEC_URL, TIMEOUT)
            elif source_mode == "fixture":
                if fetch is not None:
                    raw = fetch(SEC_URL, timeout=TIMEOUT)
                else:
                    raw = fixture_bytes
            else:
                if fixture_bytes is None:
                    raise CycleError("manual mode requires fixture bytes")
                raw = fixture_bytes
        except CycleError as e:
            return _fail(e)
        except Exception as e:
            return _fail(e)

        if not isinstance(raw, (bytes, bytearray)):
            return _fail("fetch returned non-bytes")
        raw = bytes(raw)
        if len(raw) > MAX_BYTES:
            return _fail("oversized payload %d" % len(raw))
        if len(raw) == 0:
            return _fail("empty payload")

        try:
            doc = json.loads(raw.decode("utf-8"))
        except Exception as e:
            return _fail("parse error: %s" % e)

        try:
            extracted = _extract_filings(doc)
            _validate_filings_against_now(extracted["filings"], eff_now)
        except CycleError as e:
            return _fail(e)

        _write_atomic(slot_dir / "source.json", raw)
        if crash == "after_source":
            raise Crash("injected crash after source")

        retrieved_at = eff_now.isoformat()
        receipt = {
            "slot": slot,
            "mode": "success",
            "identity": identity,
            "source": {
                "sha256": _sha(raw),
                "size": len(raw),
                "retrieved_at": retrieved_at,
                "filings": extracted["filings"],
                "cik": extracted["cik"],
                "name": extracted["name"],
                "tickers": extracted["tickers"],
            },
        }
        _write_atomic(slot_dir / "receipt.json",
                      json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8"))

        if crash == "after_receipt":
            raise Crash("injected crash after receipt")

        _copy_frozen(sim_dir, manifest_map)
        _render_html(output, cycles_dir, sim_dir, (slot, receipt))
        return {"slot": slot, "mode": "success", "reused": False}
    finally:
        try:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
        finally:
            lf.close()


def _cli(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--output", required=True)
    p.add_argument("--fixture")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--report-only", action="store_true")
    p.add_argument("--manifest")
    args = p.parse_args(argv)
    if args.refresh and args.fixture:
        raise SystemExit("refresh cannot combine with fixture")
    if args.report_only:
        res = APIcycle(args.output, manifest=args.manifest, report_only=True)
    elif args.refresh:
        res = APIcycle(args.output, refresh=True, manifest=args.manifest)
    elif args.fixture:
        res = APIcycle(args.output, now=actual_now(), fixture=args.fixture, manifest=args.manifest)
    else:
        raise SystemExit("specify --refresh, --fixture or --report-only")
    print(json.dumps(res, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
