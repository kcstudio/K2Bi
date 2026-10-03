"""Deterministic packaging of fixed repository paths from a trusted Git revision.

This module builds a *payload directory* containing the exact bytes of the
committed files listed in :data:`RUNTIME_PATHS`, read from a specific
full 40-character lowercase hex Git COMMIT object.  It never reads the
worktree and never mutates the repository.

The produced inventory is an ordinary checksum manifest, *not* an
authenticity proof.  Trust derives from comparing the exported bytes with
the same revision's Git blobs (see :func:`verify_payload`).

Only stdlib is used.  No CLI, no network, no engine imports.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Dict, List, Tuple

__all__ = ["RUNTIME_PATHS", "build_payload", "verify_payload"]


# The fixed list of repository-relative paths that may be packaged.  Order is
# the deterministic inventory order; it must not be reordered.
RUNTIME_PATHS: Tuple[str, ...] = (
    "execution/__init__.py",
    "execution/connectors/__init__.py",
    "execution/connectors/types.py",
    "execution/engine/__init__.py",
    "execution/engine/recovery.py",
    "execution/engine/recovery_context.py",
    "execution/journal/__init__.py",
    "execution/journal/reader.py",
    "execution/journal/schema.py",
    "execution/risk/__init__.py",
    "execution/risk/cash_only.py",
    "execution/risk/market_calendar.py",
    "execution/validators/__init__.py",
    "execution/validators/config.py",
    "execution/validators/config.yaml",
    "execution/validators/instrument_whitelist.py",
    "execution/validators/leverage.py",
    "execution/validators/market_hours.py",
    "execution/validators/position_size.py",
    "execution/validators/runner.py",
    "execution/validators/trade_risk.py",
    "execution/validators/types.py",
    "requirements.txt",
    "scripts/__init__.py",
    "scripts/stage2_eod/README.md",
    "scripts/stage2_eod/__init__.py",
    "scripts/stage2_eod/cli.py",
    "scripts/stage2_eod/desk.py",
    "scripts/stage2_eod/inputs.py",
    "scripts/stage2_eod/ledger.py",
    "scripts/stage2_eod/store.py",
    "scripts/stage2_eod/template.html",
    "scripts/stage2_quotes/__init__.py",
    "scripts/stage2_quotes/contract.py",
    "scripts/stage2_quotes/ibkr_adapter.py",
    "scripts/stage2_quotes/ibkr_batch.py",
    "scripts/stage3_paper/ACTIVATION.md",
    "scripts/stage3_paper/DEPLOYMENT.md",
    "scripts/stage3_paper/README.md",
    "scripts/stage3_paper/__init__.py",
    "scripts/stage3_paper/account_capture.py",
    "scripts/stage3_paper/account_readiness.py",
    "scripts/stage3_paper/action_card.py",
    "scripts/stage3_paper/capture.py",
    "scripts/stage3_paper/cash_capture.py",
    "scripts/stage3_paper/cli.py",
    "scripts/stage3_paper/desk.py",
    "scripts/stage3_paper/offline_recovery.py",
    "scripts/stage3_paper/offline_risk.py",
    "scripts/stage3_paper/packaging.py",
    "scripts/stage3_paper/preparation.py",
    "scripts/stage3_paper/quote_capture.py",
    "scripts/stage3_paper/quote_evidence.py",
    "scripts/stage3_paper/readiness.py",
    "scripts/stage3_paper/reference.py",
    "scripts/stage3_paper/research_evidence.py",
    "scripts/stage3_paper/research_proof.py",
    "scripts/stage3_paper/snapshot.py",
)

_INVENTORY_NAME = "inventory.json"
_SCHEMA = 1
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_MAX_BLOB = 2 * 1024 * 1024
_MAX_INVENTORY = 100 * 1024
_GIT_TIMEOUT = 15.0
_GIT_MODE = "100644"


class _Error(ValueError):
    """Internal controlled failure type (subclass of ValueError)."""



def _git(repo: Path, *args: str) -> bytes:
    """Run a bounded, side-effect-free Git command inside *repo*.

    Uses ``--no-pager`` and explicit ``-C`` so behaviour is independent of
    the process working directory.  The environment is trimmed of
    variables that could redirect Git to an alternate object store.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key in ("PATH", "HOME", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP")
    }
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    try:
        completed = subprocess.run(
            ["git", "--no-pager", "-C", str(repo), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_GIT_TIMEOUT,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - env dependent
        raise _Error("git invocation failed: %s" % (exc,)) from exc
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", "replace").strip()
        raise _Error("git command failed: git %s: %s" % (" ".join(args), message))
    return completed.stdout


def _validate_revision(revision: str) -> str:
    if not isinstance(revision, str) or _REVISION_RE.fullmatch(revision) is None:
        raise _Error("revision must be a full 40-character lowercase hex commit SHA")
    return revision


def _resolve_repo(repo: Path) -> Path:
    """Validate that *repo* is a directory and report its real repository root."""
    if not isinstance(repo, Path):
        raise _Error("repo must be a pathlib.Path")
    try:
        real = repo.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _Error("repository path %s is not resolvable: %s" % (repo, exc)) from exc
    if not real.is_dir():
        raise _Error("repository path %s is not a directory" % (real,))
    try:
        reported = _git(real, "rev-parse", "--show-toplevel")
    except _Error as exc:
        raise _Error("repository path %s is not a Git repository: %s" % (real, exc)) from exc
    try:
        top = Path(reported.decode("utf-8", "replace").strip()).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _Error("cannot resolve repository top level: %s" % (exc,)) from exc
    if top != real:
        raise _Error("repository path %s is not the repository root %s" % (real, top))
    return real


def _resolve_commit(repo: Path, revision: str) -> str:
    """Resolve *revision* to a COMMIT object with an identical SHA."""
    try:
        resolved = _git(repo, "rev-parse", "--verify", "%s^{commit}" % revision)
    except _Error as exc:
        raise _Error("cannot resolve revision %s to a commit: %s" % (revision, exc)) from exc
    sha = resolved.decode("utf-8", "replace").strip()
    if not _REVISION_RE.match(sha):
        raise _Error("revision %s did not resolve to a commit SHA" % (revision,))
    if sha != revision:
        raise _Error(
            "revision %s resolved to %s; a full commit SHA must resolve identically"
            % (revision, sha)
        )
    return sha


def _read_blob(repo: Path, revision: str, relpath: str) -> Tuple[bytes, Dict[str, object]]:
    """Read one committed regular blob and its mode/size metadata."""
    if "/" in relpath and any(part in ("", ".", "..") for part in relpath.split("/")):
        raise _Error("invalid path component in %r" % (relpath,))
    entry = "%s:%s" % (revision, relpath)
    try:
        meta_raw = _git(repo, "ls-tree", "-z", revision, "--", relpath)
    except _Error as exc:
        raise _Error("missing blob %s in %s: %s" % (relpath, revision, exc)) from exc
    records = [record for record in meta_raw.split(b"\x00") if record]
    if len(records) != 1:
        raise _Error("missing blob %s in %s" % (relpath, revision))
    header, _, listed = records[0].partition(b"\t")
    fields = header.split(b" ")
    if len(fields) != 3 or listed.decode("utf-8", "replace") != relpath:
        raise _Error("malformed tree entry for %s in %s" % (relpath, revision))
    mode = fields[0].decode("ascii")
    objtype = fields[1].decode("ascii")
    if objtype != "blob" or mode != _GIT_MODE:
        raise _Error(
            "path %s in %s must be a regular file with mode %s (found %s %s)"
            % (relpath, revision, _GIT_MODE, objtype, mode)
        )
    try:
        size_raw = _git(repo, "cat-file", "-s", entry)
    except _Error as exc:
        raise _Error("cannot read blob %s in %s: %s" % (relpath, revision, exc)) from exc
    try:
        reported_size = int(size_raw.decode("ascii").strip())
    except ValueError as exc:
        raise _Error("cannot read size for %s in %s" % (relpath, revision)) from exc
    if reported_size > _MAX_BLOB:
        raise _Error("blob %s in %s exceeds %d bytes" % (relpath, revision, _MAX_BLOB))
    if reported_size < 0:
        raise _Error("blob %s in %s has an invalid size" % (relpath, revision))
    try:
        data = _git(repo, "cat-file", "blob", entry)
    except _Error as exc:
        raise _Error("cannot read blob %s in %s: %s" % (relpath, revision, exc)) from exc
    if len(data) != reported_size:
        raise _Error("blob %s in %s returned wrong length" % (relpath, revision))
    return data, {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def _prepare_output(repo: Path, output_dir: Path) -> Path:
    """Validate the destination and return its resolved absolute path."""
    if not isinstance(output_dir, Path):
        raise _Error("output_dir must be a pathlib.Path")
    if ".." in output_dir.parts:
        raise _Error("output_dir must not contain '..' components")
    if not output_dir.is_absolute():
        raise _Error("output_dir must be an absolute path")
    dest = _safe_location(output_dir, "output_dir")
    parent = dest.parent
    try:
        parent_real = parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _Error("destination parent %s must exist: %s" % (parent, exc)) from exc
    if not parent_real.is_dir():
        raise _Error("destination parent %s is not a directory" % (parent_real,))
    try:
        parent_stat = parent_real.stat()
    except OSError as exc:
        raise _Error("cannot stat destination parent %s: %s" % (parent_real, exc)) from exc
    if not stat.S_ISDIR(parent_stat.st_mode):
        raise _Error("destination parent %s is not a regular directory" % (parent_real,))
    if not parent_real.is_absolute():
        raise _Error("destination parent %s must be absolute" % (parent_real,))
    if parent_real == repo or repo in parent_real.parents:
        raise _Error("output_dir must be outside the repository %s" % (repo,))
    if dest == repo or repo in dest.parents:
        raise _Error("output_dir must be outside the repository %s" % (repo,))
    if dest.exists() or os.path.islink(str(dest)):
        raise _Error("destination %s already exists" % (dest,))
    return dest


def _write_atomic(dest: Path, data: bytes) -> None:
    """Write a file with an exclusive create; no overwrite is possible."""
    try:
        fd = os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except OSError as exc:
        raise _Error("cannot create %s: %s" % (dest, exc)) from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise _Error("cannot write %s: %s" % (dest, exc)) from exc


def _inventory_bytes(inventory: Dict[str, object]) -> bytes:
    return json.dumps(inventory, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def build_payload(repo: Path, revision: str, output_dir: Path) -> dict:
    """Export the fixed paths at *revision* into a fresh *output_dir*.

    All blobs are read and hashed before any filesystem change.  The
    destination must not exist at all (not even as an empty directory).
    Only the fixed relative paths and ``inventory.json`` are created.
    """
    revision = _validate_revision(revision)
    repo_root = _resolve_repo(repo)
    commit = _resolve_commit(repo_root, revision)
    dest = _prepare_output(repo_root, output_dir)

    blobs: List[Tuple[str, bytes, Dict[str, object]]] = []
    files: Dict[str, Dict[str, object]] = {}
    for relpath in RUNTIME_PATHS:
        data, meta = _read_blob(repo_root, commit, relpath)
        blobs.append((relpath, data, meta))
        files[relpath] = meta

    inventory: Dict[str, object] = {"schema": _SCHEMA, "revision": commit, "files": files}
    inventory_raw = _inventory_bytes(inventory)

    try:
        os.mkdir(str(dest), 0o755)
    except OSError as exc:
        raise _Error("cannot create destination %s: %s" % (dest, exc)) from exc
    for relpath, _data, _meta in blobs:
        target = dest.joinpath(*relpath.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
    for relpath, data, _meta in blobs:
        _write_atomic(dest.joinpath(*relpath.split("/")), data)
    _write_atomic(dest / _INVENTORY_NAME, inventory_raw)
    return inventory


def _load_inventory(path: Path) -> Dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise _Error("inventory %s must be a regular file" % (path,))
    try:
        st = path.stat()
    except OSError as exc:
        raise _Error("cannot stat inventory %s: %s" % (path, exc)) from exc
    if not stat.S_ISREG(st.st_mode):
        raise _Error("inventory %s must be a regular file" % (path,))
    if st.st_size > _MAX_INVENTORY:
        raise _Error("inventory %s exceeds %d bytes" % (path, _MAX_INVENTORY))
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise _Error("cannot read inventory %s: %s" % (path, exc)) from exc
    if len(raw) > _MAX_INVENTORY:
        raise _Error("inventory %s exceeds %d bytes" % (path, _MAX_INVENTORY))

    duplicates: List[str] = []

    def _hook(pairs: List[Tuple[str, object]]) -> Dict[str, object]:
        seen: Dict[str, object] = {}
        for key, value in pairs:
            if key in seen:
                duplicates.append(key)
            seen[key] = value
        return seen

    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_hook)
    except UnicodeDecodeError as exc:
        raise _Error("inventory is not valid UTF-8: %s" % (exc,)) from exc
    except RecursionError as exc:
        raise _Error("inventory JSON is nested too deeply: %s" % (exc,)) from exc
    except ValueError as exc:
        raise _Error("inventory is not valid JSON: %s" % (exc,)) from exc
    if duplicates:
        raise _Error("inventory contains duplicate keys: %s" % (",".join(sorted(set(duplicates)))))
    if not isinstance(parsed, dict):
        raise _Error("inventory must be a JSON object")
    return parsed


def _scan_payload(payload_dir: Path, expected_dirs: "set") -> Dict[str, Path]:
    """Return relative POSIX path -> real path for every payload entry.

    Rejects symlinked ancestors, symlinked files/directories and any
    unexpected directory (including empty ones) outside the fixed set of
    parent directories implied by ``RUNTIME_PATHS``.
    """
    if not isinstance(payload_dir, Path):
        raise _Error("payload_dir must be a pathlib.Path")
    if ".." in payload_dir.parts:
        raise _Error("payload_dir must not contain '..' components")
    if not payload_dir.is_absolute():
        raise _Error("payload_dir must be an absolute path")
    root = _safe_location(payload_dir, "payload_dir")
    if not root.is_dir():
        raise _Error("payload_dir %s is not a directory" % (root,))
    found: Dict[str, Path] = {}
    for dirpath, dirnames, filenames in os.walk(str(root), followlinks=False):
        current = Path(dirpath)
        for name in list(dirnames):
            entry = current / name
            rel = entry.relative_to(root).as_posix()
            if entry.is_symlink():
                raise _Error("payload entry %s must not be a symlink" % (rel,))
            if rel not in expected_dirs:
                raise _Error("payload contains unexpected directory %s" % (rel,))
        for name in filenames:
            entry = current / name
            rel = entry.relative_to(root).as_posix()
            if entry.is_symlink():
                raise _Error("payload entry %s must not be a symlink" % (rel,))
            try:
                st = entry.stat()
            except OSError as exc:
                raise _Error("cannot stat payload entry %s: %s" % (rel, exc)) from exc
            if not stat.S_ISREG(st.st_mode):
                raise _Error("payload entry %s must be a regular file" % (rel,))
            found[rel] = entry
    return found


def _validate_inventory_shape(inventory: Dict[str, object], commit: str) -> Dict[str, object]:
    if set(inventory.keys()) != {"schema", "revision", "files"}:
        raise _Error("inventory must contain exactly schema, revision and files")
    schema = inventory["schema"]
    if type(schema) is not int or schema != _SCHEMA:
        raise _Error("inventory schema must be the integer %d" % (_SCHEMA,))
    if inventory["revision"] != commit:
        raise _Error("inventory revision %r does not match %s" % (inventory["revision"], commit))
    files = inventory["files"]
    if type(files) is not dict:
        raise _Error("inventory files must be an object")
    if set(files.keys()) != set(RUNTIME_PATHS):
        extra = sorted(repr(key) for key in set(files.keys()) - set(RUNTIME_PATHS))
        missing = sorted(set(RUNTIME_PATHS) - set(files.keys()))
        raise _Error("inventory file set mismatch (extra=%s missing=%s)" % (extra, missing))
    for relpath in RUNTIME_PATHS:
        entry = files[relpath]
        if type(entry) is not dict or set(entry.keys()) != {"sha256", "bytes"}:
            raise _Error("inventory entry %s is malformed" % (relpath,))
        sha = entry["sha256"]
        size = entry["bytes"]
        if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{64}", sha) is None:
            raise _Error("inventory entry %s has an invalid sha256" % (relpath,))
        if type(size) is not int or size < 0:
            raise _Error("inventory entry %s has an invalid size" % (relpath,))
    return files


def verify_payload(repo: Path, revision: str, payload_dir: Path) -> dict:
    """Compare a completed payload directory against the trusted Git revision.

    Every fixed path must exist as a regular non-symlink file whose bytes
    match the committed blob, and ``inventory.json`` must match exactly.
    Extra, missing, tampered or symlinked entries are rejected.
    """
    revision = _validate_revision(revision)
    repo_root = _resolve_repo(repo)
    commit = _resolve_commit(repo_root, revision)

    expected: Dict[str, Dict[str, object]] = {}
    expected_data: Dict[str, bytes] = {}
    for relpath in RUNTIME_PATHS:
        data, meta = _read_blob(repo_root, commit, relpath)
        expected_data[relpath] = data
        expected[relpath] = meta

    expected_dirs = set()
    for relpath in RUNTIME_PATHS:
        parts = relpath.split("/")[:-1]
        for index in range(1, len(parts) + 1):
            expected_dirs.add("/".join(parts[:index]))

    found = _scan_payload(payload_dir, expected_dirs)
    root = _safe_location(payload_dir, "payload_dir")
    if root == repo_root or repo_root in root.parents:
        raise _Error("payload must remain outside the repository")
    if root != payload_dir.resolve(strict=True):
        raise _Error("payload_dir %s is not a regular directory" % (payload_dir,))

    expected_rel = set(RUNTIME_PATHS) | {_INVENTORY_NAME}
    extra = sorted(set(found.keys()) - expected_rel)
    missing = sorted(expected_rel - set(found.keys()))
    if extra or missing:
        raise _Error("payload contents mismatch (extra=%s missing=%s)" % (extra, missing))

    for relpath in RUNTIME_PATHS:
        target = found[relpath]
        try:
            st = target.stat()
        except OSError as exc:
            raise _Error("cannot stat payload path %s: %s" % (relpath, exc)) from exc
        if st.st_size > _MAX_BLOB:
            raise _Error("payload path %s exceeds %d bytes" % (relpath, _MAX_BLOB))
        try:
            actual = target.read_bytes()
        except OSError as exc:
            raise _Error("cannot read payload path %s: %s" % (relpath, exc)) from exc
        if actual != expected_data[relpath]:
            raise _Error("payload path %s is tampered or does not match %s" % (relpath, commit))

    inventory = _load_inventory(found[_INVENTORY_NAME])
    files = _validate_inventory_shape(inventory, commit)
    for relpath in RUNTIME_PATHS:
        meta = expected[relpath]
        entry = files[relpath]
        if entry["sha256"] != meta["sha256"] or entry["bytes"] != meta["bytes"]:
            raise _Error("inventory metadata for %s does not match the commit" % (relpath,))

    try:
        canonical = (root / _INVENTORY_NAME).read_bytes()
    except OSError as exc:
        raise _Error("cannot re-read inventory: %s" % (exc,)) from exc
    expected_inventory = {"schema": _SCHEMA, "revision": commit, "files": expected}
    if canonical != _inventory_bytes(expected_inventory):
        raise _Error("inventory.json does not match the canonical serialization")

    return expected_inventory

def _safe_location(path: Path, label: str) -> Path:
    """Validate an absolute path free of '..' and of symlinked ancestors.

    Returns the canonical resolved path.  Every *original* component of the
    supplied path is inspected with ``lstat`` so that a symlinked ancestor
    is detected even though ``resolve`` would silently follow it.
    """
    if not isinstance(path, Path):
        raise _Error("%s must be a pathlib.Path" % (label,))
    if ".." in path.parts:
        raise _Error("%s must not contain '..' components" % (label,))
    if not path.is_absolute():
        raise _Error("%s must be an absolute path" % (label,))
    current = path
    ancestors = list(current.parents)
    if current.name in ("", ".", ".."):
        raise _Error("%s has an invalid final component" % (label,))
    for component in [current] + ancestors:
        if str(component) == component.anchor:
            continue
        try:
            lst = os.lstat(str(component))
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise _Error("cannot stat %s component %s: %s" % (label, component, exc)) from exc
        if stat.S_ISLNK(lst.st_mode):
            raise _Error("%s component %s must not be a symlink" % (label, component))
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise _Error("cannot resolve %s %s: %s" % (label, path, exc)) from exc
