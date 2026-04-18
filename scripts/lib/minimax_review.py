"""Standalone MiniMax M2.7 adversarial code reviewer.

Phase A MVP: working-tree scope, single-shot, JSON output validated against
Codex's review-output schema. Touches nothing in /ship or the codex plugin.
"""

import argparse
import json
import re
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

from minimax_common import (
    MinimaxError,
    chat_completion,
    extract_assistant_text,
    extract_token_usage,
)

LIB_DIR = Path(__file__).resolve().parent
REPO_ROOT = Path(
    subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"], text=True
    ).strip()
)
PROMPT_PATH = LIB_DIR / "adversarial-review.md"
SCHEMA_PATH = LIB_DIR / "review-output.schema.json"
DEFAULT_ARCHIVE_DIR = REPO_ROOT / ".minimax-reviews"

MAX_FILE_BYTES = 256 * 1024  # skip large files; M2.7 has 200K context but stay sane
BINARY_SNIFF_BYTES = 4096


def run_git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=REPO_ROOT, text=True, errors="replace"
    )


def is_binary(path: Path) -> bool:
    try:
        chunk = path.read_bytes()[:BINARY_SNIFF_BYTES]
    except OSError:
        return True
    if b"\x00" in chunk:
        return True
    return False


def gather_working_tree_context() -> tuple[str, list[str]]:
    """Return (context_text, changed_file_list) for working-tree scope.

    Includes:
      - git status --short (overview)
      - diffstat
      - diff vs HEAD for tracked changes
      - full content of each changed/untracked file (truncated if huge)
    """
    status = run_git("status", "--short")
    changed_files: list[str] = []
    for line in status.splitlines():
        if not line.strip():
            continue
        # status format: "XY path" or "XY orig -> new"
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        changed_files.append(path.strip().strip('"'))

    if not changed_files:
        return "", []

    diffstat = run_git("diff", "HEAD", "--stat")
    diff = run_git("diff", "HEAD")

    sections: list[str] = []
    sections.append("## git status --short\n```\n" + status.rstrip() + "\n```")
    if diffstat.strip():
        sections.append("## diffstat (HEAD)\n```\n" + diffstat.rstrip() + "\n```")
    if diff.strip():
        sections.append("## diff vs HEAD\n```diff\n" + diff.rstrip() + "\n```")

    sections.append("## Full file contents (changed and untracked)")
    for rel in sorted(set(changed_files)):
        path = REPO_ROOT / rel
        if not path.exists():
            sections.append(f"### {rel}\n_(deleted)_")
            continue
        if path.is_dir():
            sections.append(f"### {rel}\n_(directory)_")
            continue
        if is_binary(path):
            sections.append(f"### {rel}\n_(binary, skipped)_")
            continue
        try:
            data = path.read_bytes()
        except OSError as e:
            sections.append(f"### {rel}\n_(unreadable: {e})_")
            continue
        truncated_note = ""
        if len(data) > MAX_FILE_BYTES:
            data = data[:MAX_FILE_BYTES]
            truncated_note = f"\n_(truncated to {MAX_FILE_BYTES} bytes)_"
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")
        # Add line numbers so the model can reference line_start / line_end accurately
        numbered = "\n".join(
            f"{i + 1:5d}  {line}" for i, line in enumerate(text.splitlines())
        )
        sections.append(
            f"### {rel}{truncated_note}\n```\n{numbered}\n```"
        )

    return "\n\n".join(sections), changed_files


def build_prompt(target_label: str, focus: str, content: str, schema_text: str) -> str:
    template = PROMPT_PATH.read_text()
    return (
        template.replace("{{TARGET_LABEL}}", target_label)
        .replace("{{USER_FOCUS}}", focus or "No extra focus provided.")
        .replace("{{OUTPUT_SCHEMA}}", schema_text)
        .replace("{{REVIEW_INPUT}}", content)
    )


def extract_json_object(text: str) -> dict | None:
    """Try strict json.loads first, then regex-extract the first {...} block."""
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Strip common code-fence wrappers
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass
    # Greedy first-{ to last-} fallback
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def render_markdown(parsed: dict, model: str, usage: dict) -> str:
    verdict = parsed.get("verdict", "?")
    summary = parsed.get("summary", "(no summary)")
    findings = parsed.get("findings") or []
    next_steps = parsed.get("next_steps") or []

    findings_sorted = sorted(
        findings, key=lambda f: SEVERITY_ORDER.get(f.get("severity", "low"), 99)
    )

    badge = "APPROVE" if verdict == "approve" else "NEEDS-ATTENTION"
    lines: list[str] = []
    lines.append(f"# MiniMax {model} review -- {badge}")
    lines.append("")
    lines.append(f"**Summary:** {summary}")
    lines.append("")
    lines.append(
        f"**Tokens:** prompt={usage.get('prompt_tokens')}  "
        f"completion={usage.get('completion_tokens')}  "
        f"total={usage.get('total_tokens')}"
    )
    lines.append("")
    if not findings_sorted:
        lines.append("_No findings._")
    else:
        lines.append(f"## Findings ({len(findings_sorted)})")
        lines.append("")
        for i, f in enumerate(findings_sorted, 1):
            sev = (f.get("severity") or "?").upper()
            conf = f.get("confidence")
            conf_pct = f"{int(conf * 100)}%" if isinstance(conf, (int, float)) else "?"
            lines.append(
                f"### {i}. [{sev}] {f.get('title', '(untitled)')}  ({conf_pct} conf)"
            )
            lines.append(
                f"`{f.get('file', '?')}` lines "
                f"{f.get('line_start', '?')}-{f.get('line_end', '?')}"
            )
            lines.append("")
            lines.append(f.get("body", ""))
            rec = f.get("recommendation")
            if rec:
                lines.append("")
                lines.append(f"**Recommendation:** {rec}")
            lines.append("")
    if next_steps:
        lines.append("## Next steps")
        for step in next_steps:
            lines.append(f"- {step}")
        lines.append("")
    return "\n".join(lines)


def archive(
    archive_dir: Path,
    *,
    scope: str,
    model: str,
    parsed: dict | None,
    raw_text: str,
    prompt: str,
    response: dict,
    usage: dict,
) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    out = archive_dir / f"{ts}_{scope}.json"
    record = {
        "timestamp_utc": ts,
        "scope": scope,
        "model": model,
        "usage": usage,
        "parsed": parsed,
        "raw_text": raw_text,
        "prompt_chars": len(prompt),
        "response_id": response.get("id"),
    }
    out.write_text(json.dumps(record, indent=2, ensure_ascii=False))
    return out


def append_usage_log(archive_dir: Path, model: str, scope: str, usage: dict) -> None:
    archive_dir.mkdir(parents=True, exist_ok=True)
    log = archive_dir / "usage.log"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = (
        f"{ts}\t{model}\t{scope}\t"
        f"prompt={usage.get('prompt_tokens')}\t"
        f"completion={usage.get('completion_tokens')}\t"
        f"total={usage.get('total_tokens')}\n"
    )
    with log.open("a") as f:
        f.write(line)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Standalone MiniMax M2.7 adversarial code reviewer."
    )
    parser.add_argument(
        "--scope",
        default="working-tree",
        choices=["working-tree"],
        help="(Phase A: working-tree only)",
    )
    parser.add_argument(
        "--model",
        default="MiniMax-M2.7",
        help="MiniMax model id (default MiniMax-M2.7)",
    )
    parser.add_argument(
        "--focus",
        default="",
        help="Optional focus text passed into the adversarial template",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=16384,
        help="Max completion tokens (default 16384; 4096 truncates rich reviews)",
    )
    parser.add_argument(
        "--archive-dir",
        default=str(DEFAULT_ARCHIVE_DIR),
        help="Where to archive raw + parsed output",
    )
    parser.add_argument(
        "--no-archive",
        action="store_true",
        help="Skip writing the archive file",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit parsed JSON to stdout instead of rendered markdown",
    )
    args = parser.parse_args()

    schema_text = SCHEMA_PATH.read_text()

    print(f"[minimax-review] gathering {args.scope} context...", file=sys.stderr)
    context, changed = gather_working_tree_context()
    if not changed:
        print("[minimax-review] no working-tree changes; nothing to review.", file=sys.stderr)
        return 0
    print(
        f"[minimax-review] {len(changed)} changed files, "
        f"{len(context)} chars of context",
        file=sys.stderr,
    )

    target_label = (
        f"working tree of {REPO_ROOT.name} ({len(changed)} files changed)"
    )
    prompt = build_prompt(target_label, args.focus, context, schema_text)

    print(
        f"[minimax-review] calling {args.model} ({len(prompt)} prompt chars)...",
        file=sys.stderr,
    )
    try:
        response = chat_completion(
            model=args.model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=args.max_tokens,
            temperature=0.2,
        )
    except MinimaxError as e:
        print(f"[minimax-review] FAIL: {e}", file=sys.stderr)
        return 2

    raw_text = extract_assistant_text(response)
    usage = extract_token_usage(response)
    parsed = extract_json_object(raw_text)

    archive_dir = Path(args.archive_dir)
    if not args.no_archive:
        out = archive(
            archive_dir,
            scope=args.scope,
            model=args.model,
            parsed=parsed,
            raw_text=raw_text,
            prompt=prompt,
            response=response,
            usage=usage,
        )
        append_usage_log(archive_dir, args.model, args.scope, usage)
        print(f"[minimax-review] archived: {out.relative_to(REPO_ROOT)}", file=sys.stderr)

    if parsed is None:
        print(
            "[minimax-review] could not parse JSON from response. "
            "See archive for raw output.",
            file=sys.stderr,
        )
        if args.json:
            print(json.dumps({"error": "unparseable", "raw": raw_text}, indent=2))
        else:
            print("# MiniMax review -- UNPARSEABLE\n")
            print("Raw response (truncated to 4KB):\n")
            print("```\n" + raw_text[:4096] + "\n```")
        return 3

    if args.json:
        print(json.dumps(parsed, indent=2, ensure_ascii=False))
    else:
        print(render_markdown(parsed, args.model, usage))
    return 0


if __name__ == "__main__":
    sys.exit(main())
