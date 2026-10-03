import hashlib
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.stage3_paper import packaging as packaging_module
from scripts.stage3_paper.packaging import (
    RUNTIME_PATHS,
    build_payload,
    verify_payload,
)


ErrorType = packaging_module._Error


class PackagingTestBase(unittest.TestCase):
    """Shared synthetic Git repository fixture for packaging tests."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.base = Path(cls._tmp.name).resolve()
        cls.repo = cls.base / "repo"
        cls.repo.mkdir()
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(cls.base),
            "GIT_CONFIG_NOSYSTEM": "1",
            "LC_ALL": "C",
            "LANG": "C",
        }
        cls.env = env
        cls._git("init", "-q")
        cls._git("config", "user.email", "t@example.invalid")
        cls._git("config", "user.name", "Tester")
        cls._git("config", "commit.gpgsign", "false")
        cls.contents = {}
        for relpath in RUNTIME_PATHS:
            cls.contents[relpath] = ("data:%s\n" % relpath).encode("utf-8")
        for relpath, data in cls.contents.items():
            target = cls.repo / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        # Committed paths are made executable-then-normalized so we control mode.
        cls._git("add", "--", *RUNTIME_PATHS)
        cls._git("commit", "-q", "-m", "initial")
        cls.revision = cls._git("rev-parse", "HEAD").strip()

    @classmethod
    def _git(cls, *args, cwd=None):
        completed = subprocess.run(
            ["git", "-C", str(cwd or cls.repo), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=cls.env,
            check=True,
        )
        return completed.stdout.decode("utf-8", "replace")

    def setUp(self):
        self.case = Path(tempfile.mkdtemp(dir=str(self.base), prefix="case-"))
        self.addCleanup(self._rmtree, self.case)

    @staticmethod
    def _rmtree(path):
        import shutil

        shutil.rmtree(str(path), ignore_errors=True)

    def out(self, name="payload"):
        return self.case / name

    def built(self, name="payload"):
        dest = self.out(name)
        inventory = build_payload(self.repo, self.revision, dest)
        return dest, inventory


class BuildPayloadTests(PackagingTestBase):
    def test_build_and_verify_roundtrip(self):
        dest, inventory = self.built()
        self.assertEqual(inventory["schema"], 1)
        self.assertEqual(inventory["revision"], self.revision)
        self.assertEqual(sorted(inventory["files"]), sorted(RUNTIME_PATHS))
        self.assertEqual(inventory, verify_payload(self.repo, self.revision, dest))
        for relpath in RUNTIME_PATHS:
            written = (dest / relpath).read_bytes()
            self.assertEqual(written, self.contents[relpath])
            self.assertEqual(inventory["files"][relpath]["sha256"], hashlib.sha256(written).hexdigest())
            self.assertEqual(inventory["files"][relpath]["bytes"], len(written))

    def test_deterministic_two_fresh_outputs(self):
        first, inv1 = self.built()
        second, inv2 = self.built("second-payload")
        self.assertEqual(inv1, inv2)
        self.assertEqual((first / "inventory.json").read_bytes(), (second / "inventory.json").read_bytes())
        self.assertEqual(verify_payload(self.repo, self.revision, second), inv1)

    def test_dirty_worktree_and_untracked_excluded(self):
        # Mutate worktree and add untracked + private files after commit.
        (self.repo / RUNTIME_PATHS[0]).write_bytes(b"dirty")
        (self.repo / "private_secret.txt").write_bytes(b"secret")
        try:
            dest, inventory = self.built()
            self.assertEqual((dest / RUNTIME_PATHS[0]).read_bytes(), self.contents[RUNTIME_PATHS[0]])
            self.assertFalse((dest / "private_secret.txt").exists())
            self.assertEqual(verify_payload(self.repo, self.revision, dest), inventory)
        finally:
            (self.repo / RUNTIME_PATHS[0]).write_bytes(self.contents[RUNTIME_PATHS[0]])
            (self.repo / "private_secret.txt").unlink()

    def test_destination_rejections(self):
        for name, make, label in [
            ("empty", lambda p: p.mkdir(), "existing empty dir"),
            ("nonempty", lambda p: (p.mkdir(), (p / "x").write_bytes(b"y")), "existing nonempty dir"),
            ("file", lambda p: p.write_bytes(b"z"), "existing file"),
        ]:
            with self.subTest(label=label):
                dest = self.out(name)
                make(dest)
                before = dest.stat()
                with self.assertRaises(ErrorType):
                    build_payload(self.repo, self.revision, dest)
                after = dest.stat()
                self.assertEqual(before.st_size, after.st_size)

    def test_destination_symlink_and_ancestor(self):
        real = self.case / "real"
        real.mkdir()
        link = self.case / "link"
        os.symlink(str(real), str(link))
        with self.assertRaises(ErrorType):
            build_payload(self.repo, self.revision, link / "payload")
        with self.assertRaises(ErrorType):
            build_payload(self.repo, self.revision, link)

    def test_paths_rejected(self):
        cases = [
            (Path("relative/payload"), "relative"),
            (self.case / "a" / ".." / "payload2", "dotdot"),
            (self.repo / "payload3", "inside repo"),
        ]
        for dest, label in cases:
            with self.subTest(label=label):
                with self.assertRaises(ErrorType):
                    build_payload(self.repo, self.revision, dest)
                self.assertFalse((self.case / "payload2").exists())

    def test_invalid_revisions(self):
        for bad in (self.revision.upper(), self.revision + "\n", "HEAD", "0" * 40, "g" * 40):
            with self.subTest(bad=bad):
                with self.assertRaises(ErrorType):
                    build_payload(self.repo, bad, self.out("r-" + str(abs(hash(bad)))))

    def test_pre_read_before_destination_creation(self):
        # Remove a committed path so the blob is missing; destination must not be created.
        self._git("rm", "-q", "--cached", RUNTIME_PATHS[-1])
        self._git("commit", "-q", "-m", "drop-last")
        broken = self._git("rev-parse", "HEAD").strip()
        dest = self.out("broken")
        with self.assertRaises(ErrorType):
            build_payload(self.repo, broken, dest)
        self.assertFalse(dest.exists())
        self._git("reset", "-q", "--hard", self.revision)


class VerifyPayloadTests(PackagingTestBase):
    def test_verify_detects_tamper_missing_extra(self):
        dest, _ = self.built()
        (dest / RUNTIME_PATHS[1]).write_bytes(b"tampered")
        with self.assertRaises(ErrorType):
            verify_payload(self.repo, self.revision, dest)

    def test_verify_missing_file(self):
        dest, _ = self.built()
        (dest / RUNTIME_PATHS[2]).unlink()
        with self.assertRaises(ErrorType):
            verify_payload(self.repo, self.revision, dest)

    def test_verify_extra_file_and_dirs(self):
        dest, _ = self.built()
        (dest / "extra.bin").write_bytes(b"x")
        with self.assertRaises(ErrorType):
            verify_payload(self.repo, self.revision, dest)
        (dest / "extra.bin").unlink()
        (dest / "emptyextra").mkdir()
        with self.assertRaises(ErrorType):
            verify_payload(self.repo, self.revision, dest)

    def test_verify_inventory_metadata(self):
        dest, _ = self.built()
        inv_path = dest / "inventory.json"
        original = inv_path.read_bytes()
        data = json.loads(original.decode("utf-8"))
        data["files"][RUNTIME_PATHS[0]]["sha256"] = "0" * 64
        inv_path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ErrorType):
            verify_payload(self.repo, self.revision, dest)
        inv_path.write_bytes(original)
        self.assertEqual(verify_payload(self.repo, self.revision, dest)["revision"], self.revision)

    def test_verify_inventory_duplicate_keys(self):
        dest, _ = self.built()
        inv_path = dest / "inventory.json"
        raw = inv_path.read_text(encoding="utf-8")
        dup = raw.replace("{", '{"schema":1,"schema":1,', 1)
        inv_path.write_text(dup, encoding="utf-8")
        with self.assertRaises(ErrorType):
            verify_payload(self.repo, self.revision, dest)

    def test_verify_repeat_and_revision_arg(self):
        dest, inventory = self.built()
        self.assertEqual(verify_payload(self.repo, self.revision, dest), inventory)
        with self.assertRaises(ErrorType):
            verify_payload(self.repo, "0" * 40, dest)


if __name__ == "__main__":
    unittest.main()

class PackagingBoundaryTests(PackagingTestBase):
    def test_nonregular_and_oversized_git_blob_rejected_before_output(self):
        path = self.repo / RUNTIME_PATHS[0]
        for name, content, mode in [('large', b'x' * (2 * 1024 * 1024 + 1), 0o644), ('executable', b'normal', 0o755)]:
            with self.subTest(name=name):
                path.write_bytes(content); path.chmod(mode)
                self._git('add', '--', RUNTIME_PATHS[0]); self._git('commit', '-q', '-m', name)
                revision = self._git('rev-parse', 'HEAD').strip()
                with self.assertRaises(ValueError): build_payload(self.repo, revision, self.out(name))
                self.assertFalse(self.out(name).exists())
        self._git('reset', '-q', '--hard', self.revision)
    def test_inventory_wrong_types_depth_and_size(self):
        dest, _ = self.built(); inventory = dest / 'inventory.json'; original = inventory.read_bytes()
        doc = json.loads(original); changes = [dict(doc, schema=1.0), dict(doc, schema=True), dict(doc, files=[]), dict(doc, extra=True)]
        for value in changes:
            with self.subTest(value=str(value)[:50]):
                inventory.write_text(json.dumps(value))
                with self.assertRaises(ValueError): verify_payload(self.repo, self.revision, dest)
        for raw in (b'['*1100+b']'*1100, b'x'*102401, b'\xff'):
            inventory.write_bytes(raw)
            with self.assertRaises(ValueError): verify_payload(self.repo, self.revision, dest)
        inventory.write_bytes(original)
    def test_payload_root_ancestor_file_and_directory_symlinks(self):
        dest, _ = self.built(); link = self.case / 'root-link'; link.symlink_to(dest, target_is_directory=True)
        with self.assertRaises(ValueError): verify_payload(self.repo, self.revision, link)
        path = dest / RUNTIME_PATHS[0]; original = path.read_bytes(); path.unlink(); path.symlink_to(self.repo / RUNTIME_PATHS[0])
        with self.assertRaises(ValueError): verify_payload(self.repo, self.revision, dest)
        path.unlink(); path.write_bytes(original)
        other = dest / 'linked-dir'; other.symlink_to(self.repo, target_is_directory=True)
        with self.assertRaises(ValueError): verify_payload(self.repo, self.revision, dest)
