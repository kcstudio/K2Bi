"""Unit tests for scripts/lib/minimax_review.py Phase B scope gatherers.

Ports the K2B test suite (tests/minimax-review-scope.test.sh) to Python
unittest -- K2Bi uses unittest, K2B uses bash. Same scenarios, same
assertions, same fixture-mini-repo strategy.

Each test builds a deterministic git repo in tempfile.TemporaryDirectory()
and points the gatherer at it via repo_root=Path(tempdir). No mocks --
real git commands run against real fixture repos.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "lib"))

from minimax_review import (  # noqa: E402
    extract_json_object,
    gather_diff_scoped_context,
    gather_file_list_context,
    gather_plan_context,
    gather_working_tree_context,
    is_valid_review_object,
)


def build_fixture_repo(out: Path) -> None:
    """Initialize a fresh git repo with two committed files + one untracked."""
    out.mkdir(parents=True, exist_ok=True)

    def git(*args: str) -> None:
        subprocess.check_call(
            ["git", *args], cwd=out, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    (out / "file_a.py").write_text("def a():\n    return 1\n")
    (out / "file_b.py").write_text("def b():\n    return 2\n")
    git("add", "file_a.py", "file_b.py")
    git("commit", "-q", "-m", "init")
    (out / "extra.py").write_text("def extra():\n    return 3\n")  # untracked


class WorkingTreeRegression(unittest.TestCase):
    """Test 1: Phase A working-tree gatherer behavior is preserved."""

    def test_working_tree_regression(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            build_fixture_repo(tmp_path)
            (tmp_path / "file_a.py").write_text("def a():\n    return 99\n")
            (tmp_path / "file_b.py").unlink()  # tracked deletion

            # 1a: determinism
            out1, files1 = gather_working_tree_context(repo_root=tmp_path)
            out2, files2 = gather_working_tree_context(repo_root=tmp_path)
            self.assertEqual(out1, out2, "gatherer must be deterministic")
            self.assertEqual(files1, files2)

            # 1b: section header ordering
            expected_headers = [
                "## git status --short",
                "## diffstat (HEAD)",
                "## diff vs HEAD",
                "## Full file contents (changed and untracked)",
            ]
            last_pos = -1
            for header in expected_headers:
                pos = out1.find(header)
                self.assertNotEqual(pos, -1, f"missing header: {header}")
                self.assertGreater(pos, last_pos, f"header out of order: {header}")
                last_pos = pos

            # 1c: deleted-file marker
            self.assertIn("_(deleted)_", out1, "deleted-file marker missing")

            # 1d: untracked file included
            self.assertIn("### extra.py", out1, "untracked extra.py missing")

            # 1e: line numbering
            self.assertIn("    1  def a():", out1, "line numbers missing")
            self.assertIn("    2      return 99", out1, "line 2 missing")

            # 1f: returned file list is sorted
            self.assertEqual(files1, sorted(files1), "returned list not sorted")

    def test_clean_tree_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            build_fixture_repo(tmp_path)
            (tmp_path / "extra.py").unlink()  # eliminate untracked too
            ctx, files = gather_working_tree_context(repo_root=tmp_path)
            self.assertEqual(ctx, "", "clean tree should return empty context")
            self.assertEqual(files, [])

    def test_untracked_only_omits_diff_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            build_fixture_repo(tmp_path)
            # extra.py is untracked, file_a/file_b unmodified
            ctx, _ = gather_working_tree_context(repo_root=tmp_path)
            self.assertNotIn(
                "## diff vs HEAD",
                ctx,
                "empty diff should omit '## diff vs HEAD' section",
            )
            self.assertIn(
                "## Full file contents",
                ctx,
                "untracked-only must still produce 'Full file contents'",
            )


class DiffScoped(unittest.TestCase):
    def test_clean_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            build_fixture_repo(tmp_path)
            ctx, files = gather_diff_scoped_context(["file_a.py"], repo_root=tmp_path)
            self.assertIn("file_a.py", ctx)
            self.assertIn("    1  def a", ctx, "line numbering missing")
            self.assertNotIn("file_b.py", ctx, "file_b.py leaked into output")

    def test_excludes_unrelated_dirty_files(self) -> None:
        """The 2026-04-19 incident fix: unrelated dirty files must not leak."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            build_fixture_repo(tmp_path)
            (tmp_path / "file_a.py").write_text("def a():\n    return 99\n")
            (tmp_path / "file_b.py").write_text("def b():\n    return 99\n")
            ctx, _ = gather_diff_scoped_context(["file_a.py"], repo_root=tmp_path)
            self.assertIn("file_a.py", ctx)
            self.assertNotIn("file_b.py", ctx, "unrelated dirty file_b.py leaked")
            self.assertNotIn("extra.py", ctx, "unrelated untracked extra.py leaked")
            self.assertIn("return 99", ctx)
            self.assertIn("```diff", ctx)

    def test_returns_sorted_file_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            build_fixture_repo(tmp_path)
            _, files = gather_diff_scoped_context(
                ["file_b.py", "file_a.py"], repo_root=tmp_path
            )
            self.assertEqual(files, sorted(files))


class FileList(unittest.TestCase):
    def test_happy_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            build_fixture_repo(tmp_path)
            ctx, _ = gather_file_list_context(
                ["file_a.py", "file_b.py"], repo_root=tmp_path
            )
            self.assertIn("file_a.py", ctx)
            self.assertIn("file_b.py", ctx)
            self.assertIn("    1  def a", ctx)
            self.assertIn("    1  def b", ctx)
            self.assertNotIn("## git status", ctx, "file-list leaked git status")
            self.assertNotIn("```diff", ctx, "file-list leaked git diff")

    def test_warns_and_skips_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            build_fixture_repo(tmp_path)
            with self._capture_stderr() as err:
                ctx, _ = gather_file_list_context(
                    ["file_a.py", "missing.py"], repo_root=tmp_path
                )
            self.assertIn("file_a.py", ctx)
            self.assertNotIn("missing.py", ctx)
            self.assertIn("skipping missing file: missing.py", err.getvalue())

    def test_warns_and_skips_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            build_fixture_repo(tmp_path)
            (tmp_path / "subdir").mkdir()
            (tmp_path / "subdir" / "inner.py").write_text("inside\n")
            with self._capture_stderr() as err:
                ctx, _ = gather_file_list_context(
                    ["file_a.py", "subdir"], repo_root=tmp_path
                )
            self.assertIn("file_a.py", ctx)
            self.assertNotIn("### subdir", ctx)
            self.assertNotIn("inner.py", ctx, "gatherer should not recurse")
            self.assertIn("skipping directory: subdir", err.getvalue())

    @staticmethod
    def _capture_stderr():
        import contextlib
        import io

        buf = io.StringIO()
        return contextlib.redirect_stderr(buf) if False else _StderrCapture(buf)


class _StderrCapture:
    """Wrap an io.StringIO so it can be used as a context manager AND
    expose .getvalue() the way tests expect."""

    def __init__(self, buf):
        self._buf = buf
        self._old = None

    def __enter__(self):
        import sys
        self._old = sys.stderr
        sys.stderr = self._buf
        return self

    def __exit__(self, *exc):
        import sys
        sys.stderr = self._old

    def getvalue(self):
        return self._buf.getvalue()


class PlanScoped(unittest.TestCase):
    def test_resolves_wikilinks_and_paths(self) -> None:
        """Wikilinks via wiki/raw/ search; abs/rel paths via direct resolution."""
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as abs_tmp:
            tmp_path = Path(tmp)
            build_fixture_repo(tmp_path)
            (tmp_path / "wiki" / "concepts").mkdir(parents=True)
            (tmp_path / "scripts").mkdir()
            (tmp_path / "tests").mkdir()
            (tmp_path / "docs").mkdir()
            (tmp_path / "scripts" / "foo.py").write_text("def foo():\n    pass\n")
            (tmp_path / "tests" / "bar.test.sh").write_text("echo bar\n")
            (tmp_path / "wiki" / "concepts" / "concept_x.md").write_text("# concept x\n")
            (tmp_path / "README.md").write_text("# top-level readme\n")
            (tmp_path / "docs" / "notes.md").write_text("# nested doc\n")

            abs_fixture = Path(abs_tmp) / "abs_target.py"
            abs_fixture.write_text('def abs_func():\n    return "abs"\n')

            (tmp_path / "plan.md").write_text(
                f"""# Plan: example

References:
- [[concept_x]]
- scripts/foo.py
- tests/bar.test.sh
- README.md
- docs/notes.md
- {abs_fixture}
"""
            )

            ctx, _ = gather_plan_context("plan.md", repo_root=tmp_path)

            self.assertIn("plan.md", ctx)
            self.assertIn("wiki/concepts/concept_x.md", ctx)
            self.assertIn("scripts/foo.py", ctx)
            self.assertIn("tests/bar.test.sh", ctx)
            self.assertIn("README.md", ctx)
            self.assertIn("docs/notes.md", ctx)
            self.assertIn(str(abs_fixture), ctx)
            self.assertIn("    1  def foo", ctx)
            self.assertIn("    1  def abs_func", ctx)

    def test_warns_on_unresolvable_wikilink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            build_fixture_repo(tmp_path)
            (tmp_path / "plan.md").write_text(
                "# Plan: example\nReferences:\n- [[does-not-exist]]\n"
            )
            with FileList._capture_stderr() as err:
                ctx, _ = gather_plan_context("plan.md", repo_root=tmp_path)
            self.assertIn("plan.md", ctx)
            self.assertIn(
                "unresolvable wikilink: [[does-not-exist]]", err.getvalue()
            )
            self.assertNotIn("#### does-not-exist", ctx)
            self.assertNotIn("### Referenced files", ctx)

    def test_marks_missing_path_refs(self) -> None:
        """Spec 'mark, don't drop' rule: path-refs that resolve to missing
        files must appear in output with _(file missing)_ marker, never
        silently dropped."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            build_fixture_repo(tmp_path)
            (tmp_path / "scripts").mkdir()
            (tmp_path / "scripts" / "real.py").write_text("def real():\n    pass\n")
            (tmp_path / "plan.md").write_text(
                """# Plan: example
References:
- scripts/real.py
- scripts/missing.py
- /absolute/that/does/not/exist.py
"""
            )
            ctx, _ = gather_plan_context("plan.md", repo_root=tmp_path)
            self.assertIn("scripts/real.py", ctx)
            self.assertIn("    1  def real", ctx)
            self.assertIn(
                "scripts/missing.py", ctx, "missing path-ref must be marked"
            )
            self.assertIn("_(file missing)_", ctx)
            self.assertIn("/absolute/that/does/not/exist.py", ctx)

    def test_ignores_prose_with_slashes_no_extension(self) -> None:
        """MiniMax Checkpoint 2 HIGH-1 fix: PATH_REF_RE used to match
        slash-bearing prose like 'gather/run_git'. Now requires extension
        on relative paths."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            build_fixture_repo(tmp_path)
            (tmp_path / "scripts").mkdir()
            (tmp_path / "scripts" / "real.py").write_text("def real():\n    pass\n")
            (tmp_path / "plan.md").write_text(
                """# Plan: example
The gatherer in `gather/run_git` does the heavy lifting.
We support abs/rel paths via Path resolution.
The 'unreadable/deleted' state is marked, not dropped.
Real reference: scripts/real.py
"""
            )
            ctx, _ = gather_plan_context("plan.md", repo_root=tmp_path)
            self.assertIn("scripts/real.py", ctx)
            self.assertNotIn("#### gather/run_git", ctx)
            self.assertNotIn("#### abs/rel", ctx)
            self.assertNotIn("#### unreadable/deleted", ctx)


class CLIDispatch(unittest.TestCase):
    """Tests 10-15: CLI argparse + dispatcher behavior. Each test runs
    the script via subprocess and asserts on exit code + stderr message,
    stopping short of the actual MiniMax network call."""

    SCRIPT = REPO_ROOT / "scripts" / "lib" / "minimax_review.py"

    def _run(self, args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(self.SCRIPT), *args, "--no-archive"],
            capture_output=True,
            text=True,
            cwd=cwd,
        )

    def test_empty_files_list_exits_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            build_fixture_repo(tmp_path)
            res = self._run(
                ["--scope", "files", "--files", ",, ,"], cwd=tmp_path
            )
            self.assertEqual(res.returncode, 1, f"stderr: {res.stderr}")
            self.assertIn("parsed to empty list", res.stderr)
            res2 = self._run(
                ["--scope", "diff", "--files", ",, ,"], cwd=tmp_path
            )
            self.assertEqual(res2.returncode, 1)

    def test_scope_plan_requires_plan(self) -> None:
        res = self._run(["--scope", "plan"])
        self.assertEqual(res.returncode, 1)
        self.assertIn("requires --plan", res.stderr)

    def test_scope_diff_requires_files(self) -> None:
        res = self._run(["--scope", "diff"])
        self.assertEqual(res.returncode, 1)
        self.assertIn("requires --files", res.stderr)

    def test_scope_files_requires_files(self) -> None:
        res = self._run(["--scope", "files"])
        self.assertEqual(res.returncode, 1)
        self.assertIn("requires --files", res.stderr)

    def test_argparse_rejects_invalid_scope(self) -> None:
        res = self._run(["--scope", "bogus"])
        self.assertNotEqual(res.returncode, 0)

    def test_default_scope_is_working_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            build_fixture_repo(tmp_path)
            (tmp_path / "extra.py").unlink()  # eliminate untracked
            res = self._run([], cwd=tmp_path)
            self.assertEqual(res.returncode, 0)
            self.assertIn("no working-tree changes", res.stderr)
            self.assertIn("gathering working-tree context", res.stderr)


class JSONExtractionValidation(unittest.TestCase):
    """Regression: a partial JSON parse must NOT silently exit 0 with
    `findings: []`.

    Codex flagged this twice during the m2.22 review window. The fence
    path uses json.JSONDecoder.raw_decode, which stops at the end of
    the first valid JSON value -- so an early ```json stub before the
    real review, or a truncated response that fences only
    `{"verdict": "approve"}`, parses successfully and renders as a
    successful empty review. The wrapper's quality gate at
    review_runner.py:368-382 only checks for verdict markers on rc=0,
    so the malformed-success suppresses the secondary-reviewer fallback.

    The fix: validate the parsed object has the schema-required fields
    (verdict + summary + findings + next_steps) before treating it as
    a review. Invalid -> treat as unparseable, exit non-zero, let the
    wrapper fall through.
    """

    def test_early_fence_stub_with_later_real_review_returns_none(self) -> None:
        """The original R1 bug: an early ```json stub followed by a
        later ```json review used to silently drop the real review.
        After R7 (multiple ```json fences are ambiguous),
        extract_json_object rejects the entire response so the
        wrapper falls through to the secondary reviewer."""
        text = (
            "Let me start the review.\n\n"
            "```json\n"
            '{"verdict": "approve"}\n'
            "```\n\n"
            "Wait, on second look I have findings:\n\n"
            "```json\n"
            '{"verdict": "needs-attention", "summary": "real review", '
            '"findings": [{"severity": "high", "title": "X", '
            '"body": "detail", "file": "a.py", "line_start": 1, '
            '"line_end": 2, "confidence": 0.9, "recommendation": "fix"}], '
            '"next_steps": []}\n'
            "```\n"
        )
        self.assertIsNone(extract_json_object(text))

    def test_validator_rejects_verdict_only_stub(self) -> None:
        self.assertFalse(is_valid_review_object({"verdict": "approve"}))

    def test_validator_rejects_missing_findings(self) -> None:
        self.assertFalse(
            is_valid_review_object(
                {
                    "verdict": "approve",
                    "summary": "looks good",
                    "next_steps": [],
                }
            )
        )

    def test_validator_rejects_missing_summary(self) -> None:
        self.assertFalse(
            is_valid_review_object(
                {
                    "verdict": "approve",
                    "findings": [],
                    "next_steps": [],
                }
            )
        )

    def test_validator_rejects_unknown_verdict(self) -> None:
        self.assertFalse(
            is_valid_review_object(
                {
                    "verdict": "unparseable",
                    "summary": "?",
                    "findings": [],
                    "next_steps": [],
                }
            )
        )

    def test_validator_rejects_non_list_findings(self) -> None:
        self.assertFalse(
            is_valid_review_object(
                {
                    "verdict": "approve",
                    "summary": "ok",
                    "findings": "none",
                    "next_steps": [],
                }
            )
        )

    def test_validator_accepts_empty_findings(self) -> None:
        self.assertTrue(
            is_valid_review_object(
                {
                    "verdict": "approve",
                    "summary": "no issues",
                    "findings": [],
                    "next_steps": [],
                }
            )
        )

    def test_validator_accepts_full_review(self) -> None:
        self.assertTrue(
            is_valid_review_object(
                {
                    "verdict": "needs-attention",
                    "summary": "found issues",
                    "findings": [
                        {
                            "severity": "high",
                            "title": "X",
                            "body": "detail",
                            "file": "a.py",
                            "line_start": 1,
                            "line_end": 2,
                            "confidence": 0.9,
                            "recommendation": "fix",
                        }
                    ],
                    "next_steps": ["follow up"],
                }
            )
        )

    # R2 (Codex post-commit review): top-level shape is not enough.
    # The fence path's raw_decode could land on a payload whose findings
    # items are empty objects or whose next_steps items are empty
    # strings, and the previous validator passed it -- which still hits
    # the wrapper's verdict-marker gate at rc=0 and skips fallback.
    # Mirror the schema's nested `required` for findings items and the
    # `minLength: 1` constraint for next_steps items.

    def _full_finding(self, **overrides: object) -> dict:
        base = {
            "severity": "high",
            "title": "X",
            "body": "detail",
            "file": "a.py",
            "line_start": 1,
            "line_end": 2,
            "confidence": 0.9,
            "recommendation": "fix",
        }
        base.update(overrides)
        return base

    def _wrap(self, **overrides: object) -> dict:
        base = {
            "verdict": "needs-attention",
            "summary": "ok",
            "findings": [self._full_finding()],
            "next_steps": ["next"],
        }
        base.update(overrides)
        return base

    def test_validator_rejects_empty_finding_object(self) -> None:
        self.assertFalse(is_valid_review_object(self._wrap(findings=[{}])))

    def test_validator_rejects_finding_missing_one_required_field(self) -> None:
        partial = self._full_finding()
        del partial["body"]
        self.assertFalse(is_valid_review_object(self._wrap(findings=[partial])))

    def test_validator_rejects_finding_with_invalid_severity(self) -> None:
        bad = self._full_finding(severity="weak")
        self.assertFalse(is_valid_review_object(self._wrap(findings=[bad])))

    def test_validator_rejects_finding_with_empty_title(self) -> None:
        bad = self._full_finding(title="")
        self.assertFalse(is_valid_review_object(self._wrap(findings=[bad])))

    def test_validator_rejects_finding_with_non_int_line_start(self) -> None:
        bad = self._full_finding(line_start="1")
        self.assertFalse(is_valid_review_object(self._wrap(findings=[bad])))

    def test_validator_rejects_finding_with_zero_line_start(self) -> None:
        bad = self._full_finding(line_start=0)
        self.assertFalse(is_valid_review_object(self._wrap(findings=[bad])))

    def test_validator_rejects_finding_with_out_of_range_confidence(self) -> None:
        bad = self._full_finding(confidence=1.5)
        self.assertFalse(is_valid_review_object(self._wrap(findings=[bad])))

    def test_validator_rejects_finding_with_negative_confidence(self) -> None:
        bad = self._full_finding(confidence=-0.1)
        self.assertFalse(is_valid_review_object(self._wrap(findings=[bad])))

    def test_validator_rejects_finding_with_non_dict_item(self) -> None:
        self.assertFalse(
            is_valid_review_object(self._wrap(findings=["not-a-dict"]))
        )

    def test_validator_rejects_empty_next_step_string(self) -> None:
        self.assertFalse(is_valid_review_object(self._wrap(next_steps=[""])))

    def test_validator_rejects_non_string_next_step(self) -> None:
        self.assertFalse(is_valid_review_object(self._wrap(next_steps=[1])))

    def test_validator_accepts_int_confidence_in_range(self) -> None:
        # Schema allows number; an int in [0,1] is valid (e.g. 1).
        good = self._full_finding(confidence=1)
        self.assertTrue(is_valid_review_object(self._wrap(findings=[good])))

    # R3 (Codex post-R2 review): two more soundness gaps.
    # 1. The schema sets additionalProperties: false at both the top
    #    level and inside each finding -- extra keys are schema
    #    violations and should fail validation.
    # 2. Python's json.loads accepts NaN / Infinity by default; both
    #    `NaN < 0` and `NaN > 1` are false, so the [0,1] range check
    #    silently lets non-finite confidences through.

    def test_validator_rejects_extra_top_level_key(self) -> None:
        bad = self._wrap()
        bad["extra"] = "not allowed"
        self.assertFalse(is_valid_review_object(bad))

    def test_validator_rejects_extra_finding_key(self) -> None:
        bad = self._full_finding()
        bad["extra"] = "not allowed"
        self.assertFalse(is_valid_review_object(self._wrap(findings=[bad])))

    def test_validator_rejects_nan_confidence(self) -> None:
        bad = self._full_finding(confidence=float("nan"))
        self.assertFalse(is_valid_review_object(self._wrap(findings=[bad])))

    def test_validator_rejects_inf_confidence(self) -> None:
        bad = self._full_finding(confidence=float("inf"))
        self.assertFalse(is_valid_review_object(self._wrap(findings=[bad])))

    def test_validator_rejects_negative_inf_confidence(self) -> None:
        bad = self._full_finding(confidence=float("-inf"))
        self.assertFalse(is_valid_review_object(self._wrap(findings=[bad])))

    # R4 (Codex post-R3 review): the fence path's raw_decode returns
    # the FIRST valid JSON object inside ```json ... ``` and ignores
    # everything after it within the same fence. So trailing garbage
    # or two concatenated objects in one fence still parses to the
    # first object, which is a malformed-success path even after the
    # validator hardening. Require the rest of the fenced payload to
    # be whitespace.

    def test_fence_with_clean_payload_still_parses(self) -> None:
        text = (
            "```json\n"
            '{"verdict":"approve","summary":"ok","findings":[],'
            '"next_steps":[]}\n'
            "```\n"
        )
        parsed = extract_json_object(text)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.get("verdict"), "approve")

    def test_fence_with_prose_after_closing_fence_returns_none(self) -> None:
        # R9: Codex flagged that prose after the closing fence could
        # contain real review content the parser would silently drop.
        # Per the prompt's "no prose before or after the JSON object"
        # contract, anything but whitespace after the closing fence
        # rejects the parse so the wrapper falls through.
        text = (
            "```json\n"
            '{"verdict":"approve","summary":"ok","findings":[],'
            '"next_steps":[]}\n'
            "```\n"
            "Some prose after the closing fence is NOT allowed.\n"
        )
        self.assertIsNone(extract_json_object(text))

    def test_fence_with_trailing_garbage_inside_fence_returns_none(self) -> None:
        text = (
            "```json\n"
            '{"verdict":"approve","summary":"ok","findings":[],'
            '"next_steps":[]}\n'
            "BROKEN trailing garbage inside the fence\n"
            "```\n"
        )
        self.assertIsNone(extract_json_object(text))

    def test_fence_with_two_concatenated_objects_returns_none(self) -> None:
        text = (
            "```json\n"
            '{"verdict":"approve"}\n'
            '{"verdict":"needs-attention","summary":"x","findings":[],'
            '"next_steps":[]}\n'
            "```\n"
        )
        self.assertIsNone(extract_json_object(text))

    # R5 (Codex post-R4 review): the fence regex matched
    # `\`\`\`(?:json)?` -- the `json` tag was optional, so an earlier
    # ``` ```python ... ``` `` block containing a schema-shaped stub
    # could hijack extraction even when a later real ``` ```json ```
    # block contained the actual review. Require an explicit `json`
    # tag so non-JSON fenced blocks are skipped.

    def test_python_fence_then_json_review_returns_none_after_r10(
        self,
    ) -> None:
        # Originally R5 made this case parse the real json review. R10
        # tightened further: ANY non-whitespace before the ```json
        # fence rejects, so a preceding python fence (with or without
        # a stub inside) now causes the whole response to fail. The
        # wrapper falls through to the secondary reviewer.
        text = (
            "```python\n"
            '# example output:\n'
            '# {"verdict":"approve","summary":"stub","findings":[],'
            '"next_steps":[]}\n'
            "```\n\n"
            "Real review:\n\n"
            "```json\n"
            '{"verdict":"needs-attention","summary":"real","findings":[],'
            '"next_steps":["follow up"]}\n'
            "```\n"
        )
        self.assertIsNone(extract_json_object(text))

    def test_no_fence_inline_json_with_prose_returns_none_strict(self) -> None:
        # R5 originally allowed this; R9 tightened to require the
        # response to be only the JSON (with optional whitespace).
        text = (
            'Here is the review: {"verdict":"approve","summary":"ok",'
            '"findings":[],"next_steps":[]} -- end.'
        )
        self.assertIsNone(extract_json_object(text))

    # R6 (Codex post-R5 review): the fence regex `\`\`\`json\s*` was
    # still a prefix match -- `\s*` consumes zero chars, so it matched
    # `\`\`\`jsonc` and `\`\`\`json5` and the parser would scan into
    # the wrong fence. Require a word boundary after `json`.

    def test_jsonc_fence_then_json_review_returns_none_after_r10(
        self,
    ) -> None:
        # Originally R6 (word-boundary fix) made this parse the json
        # review, skipping the jsonc prefix. R10 tightened further:
        # any leading content rejects.
        text = (
            "```jsonc\n"
            '// example:\n'
            '{"verdict":"approve","summary":"stub","findings":[],'
            '"next_steps":[]}\n'
            "```\n\n"
            "Real review:\n\n"
            "```json\n"
            '{"verdict":"needs-attention","summary":"real","findings":[],'
            '"next_steps":["follow up"]}\n'
            "```\n"
        )
        self.assertIsNone(extract_json_object(text))

    def test_json5_fence_then_json_review_returns_none_after_r10(
        self,
    ) -> None:
        text = (
            "```json5\n"
            '{"verdict":"approve","summary":"stub","findings":[],'
            '"next_steps":[]}\n'
            "```\n\n"
            "```json\n"
            '{"verdict":"needs-attention","summary":"real","findings":[],'
            '"next_steps":["follow up"]}\n'
            "```\n"
        )
        self.assertIsNone(extract_json_object(text))

    def test_json_fence_with_no_trailing_whitespace_still_parses(self) -> None:
        # Edge case: `\`\`\`json{...}` with no whitespace between the
        # tag and the `{`. Word-boundary `\b` matches at the json/{
        # transition, so this is still treated as a json fence.
        text = '```json{"verdict":"approve","summary":"ok","findings":[],"next_steps":[]}```'
        parsed = extract_json_object(text)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.get("verdict"), "approve")

    # R7 (Codex post-R6 review): the first valid `\`\`\`json` fence
    # still won unconditionally, even when a later `\`\`\`json` fence
    # held the real review (e.g. Kimi self-corrects: writes a draft,
    # then writes a real one). Ambiguity must trigger fallback.

    def test_two_json_fences_disagreeing_returns_none(self) -> None:
        text = (
            "```json\n"
            '{"verdict":"approve","summary":"draft","findings":[],'
            '"next_steps":[]}\n'
            "```\n\n"
            "Actually, on reflection:\n\n"
            "```json\n"
            '{"verdict":"needs-attention","summary":"real","findings":[],'
            '"next_steps":["follow up"]}\n'
            "```\n"
        )
        self.assertIsNone(extract_json_object(text))

    def test_two_json_fences_identical_still_returns_none(self) -> None:
        # Even if both fences contain the same JSON, the duplication
        # is itself a malformed-success signal. Reject.
        body = (
            '{"verdict":"approve","summary":"ok","findings":[],'
            '"next_steps":[]}'
        )
        text = f"```json\n{body}\n```\n\n```json\n{body}\n```\n"
        self.assertIsNone(extract_json_object(text))

    def test_single_json_fence_with_leading_non_json_fence_returns_none(
        self,
    ) -> None:
        # Pre-R10 this passed (json fence after python fence parsed).
        # R10 strict policy: leading content before the json fence
        # rejects.
        text = (
            "```python\n"
            "print('hello')\n"
            "```\n\n"
            "```json\n"
            '{"verdict":"approve","summary":"ok","findings":[],'
            '"next_steps":[]}\n'
            "```\n"
        )
        self.assertIsNone(extract_json_object(text))

    # R8 (Codex post-R7 review): the greedy first-{ to last-} fallback
    # runs when there are zero `\`\`\`json` fences, but the response
    # may still contain a schema-shaped stub inside a non-JSON fence
    # (e.g. ``` ```python ... {stub} ... ``` ```). Greedy would parse
    # the stub and the validator would accept it. Skip greedy
    # whenever any backtick fence exists -- inline-JSON-with-prose is
    # still allowed when no fence is present.

    def test_python_fence_stub_with_no_json_fence_returns_none(self) -> None:
        text = (
            "```python\n"
            '# example output:\n'
            '{"verdict":"approve","summary":"stub","findings":[],'
            '"next_steps":[]}\n'
            "```\n\n"
            "Real review in prose: I see no issues.\n"
        )
        self.assertIsNone(extract_json_object(text))

    def test_no_fence_inline_json_with_prose_returns_none(self) -> None:
        # R9: Codex flagged that surrounding prose could contain real
        # review content. Strict policy: only whole-response JSON
        # (no fences, no prose) parses via the greedy path; anything
        # else rejects so the wrapper falls through.
        text = (
            "Here is my review.\n\n"
            '{"verdict":"approve","summary":"ok","findings":[],'
            '"next_steps":[]}\n\n'
            "Hope that helps!\n"
        )
        self.assertIsNone(extract_json_object(text))

    def test_no_fence_pure_json_response_still_works(self) -> None:
        # R9 sanity: response that is just JSON (with optional
        # surrounding whitespace, the prompt's expected output shape)
        # still parses cleanly via strict json.loads.
        text = (
            '\n  {"verdict":"approve","summary":"ok","findings":[],'
            '"next_steps":[]}\n  '
        )
        parsed = extract_json_object(text)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.get("verdict"), "approve")

    # R10 (Codex post-R9 review): the fenced path also accepted
    # leading prose -- both before the ```json fence and between the
    # tag and the opening brace. Whole-response policy: leading
    # content must be whitespace too.

    def test_fence_with_prose_before_opening_fence_returns_none(self) -> None:
        text = (
            "Intro text describing what's about to come.\n\n"
            "```json\n"
            '{"verdict":"approve","summary":"ok","findings":[],'
            '"next_steps":[]}\n'
            "```\n"
        )
        self.assertIsNone(extract_json_object(text))

    def test_fence_with_prose_between_tag_and_brace_returns_none(self) -> None:
        text = (
            "```json\n"
            "// preamble inside the fence before the JSON\n"
            '{"verdict":"approve","summary":"ok","findings":[],'
            '"next_steps":[]}\n'
            "```\n"
        )
        self.assertIsNone(extract_json_object(text))

    def test_fence_with_only_whitespace_before_opens_still_parses(self) -> None:
        text = (
            "\n  \n"
            "```json\n"
            '{"verdict":"approve","summary":"ok","findings":[],'
            '"next_steps":[]}\n'
            "```"
        )
        parsed = extract_json_object(text)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.get("verdict"), "approve")


class MainExitCodeOnPartialParse(unittest.TestCase):
    """End-to-end: feed the script a malformed Kimi response that contains
    a valid verdict marker, and assert it does NOT exit 0 with
    findings: []. This is the architect's regression guard for the
    R3-r3 / R4-r4 finding -- if the script exits 0, the wrapper's
    quality gate at review_runner.py:368-382 sees `"verdict"` in the
    log and skips the Kimi <-> Codex fallback entirely, dropping all
    real findings on the floor.
    """

    SCRIPT = REPO_ROOT / "scripts" / "lib" / "minimax_review.py"

    def _run_with_mocked_response(
        self,
        raw_response: str,
        cwd: Path,
    ) -> subprocess.CompletedProcess:
        """Run main() with chat_completion patched to return raw_response.

        `python3 script.py` puts the script's directory at sys.path[0]
        before PYTHONPATH entries, so a shadow-import shim does not
        actually shadow. Instead we write a small runner that imports
        minimax_review and rebinds the in-module chat_completion symbol
        before calling main(). Cwd is the tmp fixture repo so the
        module-level `git rev-parse --show-toplevel` lands on it.
        """
        lib_dir = REPO_ROOT / "scripts" / "lib"
        runner = cwd / "_test_runner.py"
        canned = {
            "id": "stub",
            "choices": [{"message": {"content": raw_response}}],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        }
        runner.write_text(
            "import json\n"
            "import sys\n"
            f"sys.path.insert(0, {str(lib_dir)!r})\n"
            "import minimax_review\n"
            f"_CANNED = {json.dumps(canned)}\n"
            "def _fake_chat(*args, **kwargs):\n"
            "    return _CANNED\n"
            "minimax_review.chat_completion = _fake_chat\n"
            "sys.argv = ['minimax_review.py'] + sys.argv[1:]\n"
            "sys.exit(minimax_review.main())\n"
        )
        return subprocess.run(
            [
                sys.executable,
                str(runner),
                "--scope",
                "working-tree",
                "--no-archive",
                "--json",
            ],
            capture_output=True,
            text=True,
            cwd=cwd,
        )

    def test_partial_parse_with_verdict_marker_is_not_exit_0(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            build_fixture_repo(tmp_path)
            # Malformed Kimi response: an early fenced verdict-only stub
            # followed by what was supposed to be the real review with
            # embedded quotes that break the JSON. The first fence
            # parses cleanly via raw_decode and would silently win
            # without the new validator.
            raw = (
                "Starting review.\n\n"
                "```json\n"
                '{"verdict": "approve"}\n'
                "```\n\n"
                "Actually, more findings:\n\n"
                "```json\n"
                '{"verdict": "needs-attention", "summary": "real review", '
                '"findings": [{"severity": "high", "title": "Quote issue", '
                '"body": "Code has "embedded" quotes", "file": "a.py", '
                '"line_start": 1, "line_end": 2, "confidence": 0.8, '
                '"recommendation": "escape them"}], "next_steps": []}\n'
                "```\n"
            )
            res = self._run_with_mocked_response(raw, tmp_path)
            self.assertNotEqual(
                res.returncode,
                0,
                f"script exited 0 on partial-parse with verdict marker; "
                f"this would suppress the wrapper's secondary-reviewer "
                f"fallback. stdout={res.stdout!r} stderr={res.stderr!r}",
            )

    def test_truncated_verdict_only_response_is_not_exit_0(self) -> None:
        """If max_tokens cuts the response off after just the verdict,
        we must still not return success with empty findings."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            build_fixture_repo(tmp_path)
            raw = "```json\n" '{"verdict": "approve"}\n' "```\n"
            res = self._run_with_mocked_response(raw, tmp_path)
            self.assertNotEqual(
                res.returncode,
                0,
                f"script exited 0 on truncated verdict-only response; "
                f"stdout={res.stdout!r} stderr={res.stderr!r}",
            )


if __name__ == "__main__":
    unittest.main()
