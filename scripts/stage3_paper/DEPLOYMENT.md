# Isolated Stage 3 payload: preparation only

This package has not been deployed. It does not activate an engine, service, automatic refresh or trading. Use a separate versioned desk directory, never the dirty working tree or the existing VPS engine checkout. The previous prepared payload and its inventory remain immutable in a separate retained version.

## Exact clean Git export

After the code revision is independently reviewed and committed, use the stdlib packaging API with its full 40-character commit SHA:

```python
from pathlib import Path
from scripts.stage3_paper.packaging import build_payload, verify_payload

repo = Path('/absolute/path/to/K2Bi').resolve()
revision = 'REPLACE_WITH_REVIEWED_FULL_COMMIT_SHA'
destination = Path('/absolute/resolved/parent/new-stage3-version')
inventory = build_payload(repo, revision, destination)
assert verify_payload(repo, revision, destination) == inventory
```

The destination must not exist, even as an empty directory. Its parent must already exist. It must be absolute, resolved, outside the repository and free of symlink components. Every blob is read and checked before destination creation. Existing files are never overwritten or cleaned up. Verification compares both the payload bytes and inventory with trusted Git blobs, rejecting missing, extra, tampered and symlink entries. A checksum file alone is not authenticity proof.

The fixed `RUNTIME_PATHS` tuple is the payload authority. It includes the accepted EOD renderer, historical parsers, Stage 3 display/evidence/offline preparation modules, dormant read-only collectors, requirements and documentation. The reused execution files are only pure validator/data/reconciliation/read helpers plus the unchanged config. No engine main, submission connector, journal writer, circuit/kill control, service, hook, Git metadata, private input or proposal is included. Committed initializer bytes are used even when local initializer files are dirty. Packaging does not contact the broker or SEC.

## Runtime and private inputs

Use Python 3.12 and the reviewed project requirements. Rendering and offline checks need PyYAML and exchange_calendars with their pandas/numpy dependencies. The dormant collectors additionally need the existing ib_async runtime only if a later finite capture is separately authorized. No installation, subscription or runtime setup is performed by export. Record the actual installed versions alongside the prepared inventory, rather than treating version ranges as an environment lock.

The CLI reads the bundled unchanged `execution/validators/config.yaml` as a read-only context input. Compare its SHA-256 fingerprint. Neither copied limits nor an offline fixture pass grants a mandate or trading approval. The display API can receive explicit config bytes; omitted bytes use that fixed read-only runtime file.

Saved holdings, cash, account/orders, prices, source reports and acquisition proofs remain separate private operator inputs. Preserve exact bytes, account identity, request settings, source hash and independent timestamps. Never add them to Git or this payload. Do not bundle test fixtures as actual evidence. Research-reference URLs are passive unless a separately authorized finite acquisition is performed.

Use the CLI only with a fresh HTML output path and an accepted EOD receipt state. Source files and protected code/evidence paths cannot be output destinations. `--risk-context` and `--recovery-context` are separate strict supplied inputs. Recorded or missing evidence stays WAIT; complete invented examples may display estimated USD amounts, but cannot approve, fill, submit or apply anything.

## Acceptance and rollback preparation

The clean-export gate tests the unchanged production rules through the new adapters, accepted EOD compatibility, actual WAIT, invented preview, deterministic repeated calls and reconstructed inputs in the same process and invalid-input families. Packaging tests committed-byte export despite dirty/untracked files and rejects collision, symlink, missing, extra and tampered payloads. Private source preservation is checked separately without uploading private bytes to reviewers. These tests do not establish sustained operation or a real service restart.

Store the inventory with its exact Git revision and actual runtime versions. Verify again before any separately approved installation. Keep the preceding directory and inventory for rollback. A future approved desk deployment may switch an isolated desk directory after integrity/offline checks, then restore the preceding directory on failure. No shared-checkout rsync, engine/service change or kill-state edit belongs to this preparation.

The legacy deployment script can affect unrelated payload categories and dirty baseline bytes. It is not invoked here. A concrete isolated installation command, host path, permissions, dependency environment and rollback operation must be reviewed and separately authorized before deployment. See [ACTIVATION.md](ACTIVATION.md) for the actual evidence holds and proposed operating decision. Full operational Stage 3 is not complete.
