#!/usr/bin/env python3
"""Cutover: remove the OLD Hermes devin-acp integration (June 2026 source patch).

Dry run unless --apply is given. Every action prints one line:
  OK     done (apply)            WOULD  what --apply would do (dry run)
  SKIP   already clean           INFO   informational only
  FAIL   refused or failed       -> exit 2; a human must look

Steps:
  a     git checkout -- the MODIFIED patched files (untracked files are left alone)
  b     delete agent/devin_acp_client.py if it is untracked (a tracked copy is refused)
  c     remove skills/autonomous-ai-agents/devin-acp-upgrade-reapply
  d     move devin-acp-reuse-bundle into <repo>/legacy/ (never overwrites)
  e     syntax-check the patched files with the Hermes venv (in memory, no .pyc written)
  post  report residual devin_acp / devin-acp references in the tracked Hermes tree

Never touches auth.json, caches, or the model cache.
Exit: 0 clean or done, 1 error, 2 needs attention.

Examples:
  python3 scripts/remove_devin_acp.py                  # dry run against $HERMES_HOME or ~/.hermes
  python3 scripts/remove_devin_acp.py --apply          # perform the cutover
  python3 scripts/remove_devin_acp.py --hermes-home /tmp/test-hermes --legacy-dir /tmp/test-legacy
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

PATCHED_FILES: tuple[str, ...] = (
    "agent/agent_init.py",
    "agent/agent_runtime_helpers.py",
    "agent/auxiliary_client.py",
    "agent/conversation_loop.py",
    "hermes_cli/auth.py",
    "hermes_cli/model_normalize.py",
    "hermes_cli/model_switch.py",
    "hermes_cli/models.py",
    "hermes_cli/providers.py",
    "hermes_cli/runtime_provider.py",
)
CLIENT_FILE = "agent/devin_acp_client.py"
SKILL_REL = Path("skills", "autonomous-ai-agents", "devin-acp-upgrade-reapply")
BUNDLE_NAME = "devin-acp-reuse-bundle"
REF_PATTERN = "devin_acp|devin-acp"
REPO_ROOT = Path(__file__).resolve().parent.parent

# Compiled in memory so nothing (no __pycache__) is written into the Hermes checkout.
_COMPILE_SNIPPET = (
    "import pathlib, sys\n"
    "bad = []\n"
    "for name in sys.argv[1:]:\n"
    "    try:\n"
    "        compile(pathlib.Path(name).read_bytes(), name, 'exec', dont_inherit=True)\n"
    "    except SyntaxError as exc:\n"
    "        bad.append(f'{name}: {exc}')\n"
    "for line in bad:\n"
    "    print(line)\n"
    "sys.exit(1 if bad else 0)\n"
)


class Report:
    def __init__(self) -> None:
        self.attention = 0

    @staticmethod
    def line(tag: str, msg: str) -> None:
        print(f"  {tag:<6}{msg}")

    def fail(self, msg: str) -> None:
        self.attention += 1
        self.line("FAIL", msg)


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def is_own_checkout(hermes: Path, rep: Report) -> bool:
    """True only if hermes-agent is its own git repo (never a parent repo such as $HOME)."""
    top = git(hermes, "rev-parse", "--show-toplevel")
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != hermes.resolve():
        rep.fail(f"{hermes} is not its own git checkout; git steps refused")
        return False
    return True


def step_a(hermes: Path | None, apply: bool, rep: Report) -> None:
    print("[a] restore modified patched Hermes files (git checkout --)")
    if hermes is None:
        rep.line("SKIP", "no verified git checkout")
        return
    st = git(hermes, "status", "--porcelain", "--", *PATCHED_FILES)
    if st.returncode != 0:
        raise RuntimeError(f"git status failed in {hermes}: {st.stderr.strip()}")
    targets: list[tuple[str, str]] = []
    for raw in st.stdout.splitlines():
        code, path = raw[:2], raw[3:]
        if code == "??":
            rep.line("SKIP", f"{path} is untracked (nothing to restore); left alone")
        else:
            targets.append((code.strip() or "?", path))
    if not targets:
        rep.line("SKIP", "all patched files already clean")
        return
    paths = [p for _, p in targets]
    if not apply:
        stat = git(hermes, "diff", "--stat", "HEAD", "--", *paths)
        for out in (stat.stdout.rstrip() or stat.stderr.rstrip()).splitlines():
            print(f"        {out}")
        for code, path in targets:
            rep.line("WOULD", f"restore {path} (status {code})")
        return
    for code, path in targets:
        r = git(hermes, "checkout", "--", path)
        if r.returncode != 0:
            rep.fail(f"git checkout failed for {path}: {r.stderr.strip()}")
            continue
        left = git(hermes, "status", "--porcelain", "--", path).stdout.strip()
        if left:
            rep.fail(f"{path} still differs after checkout (staged change?): {left}")
        else:
            rep.line("OK", f"restored {path}")


def step_b(hermes: Path | None, apply: bool, rep: Report) -> None:
    print(f"[b] delete untracked {CLIENT_FILE}")
    if hermes is None:
        rep.line("SKIP", "no verified git checkout")
        return
    target = hermes / CLIENT_FILE
    if not (target.exists() or target.is_symlink()):
        rep.line("SKIP", "already absent")
        return
    if git(hermes, "ls-files", "--error-unmatch", "--", CLIENT_FILE).returncode == 0:
        rep.fail(f"{CLIENT_FILE} is tracked by git; refusing to delete it")
        return
    if apply:
        target.unlink()
        rep.line("OK", f"deleted {target}")
    else:
        rep.line("WOULD", f"delete {target} (untracked)")


def step_c(home: Path, apply: bool, rep: Report) -> None:
    print("[c] remove skill devin-acp-upgrade-reapply")
    target = home / SKILL_REL
    if not (target.exists() or target.is_symlink()):
        rep.line("SKIP", "already absent")
        return
    if apply:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
        rep.line("OK", f"removed {target}")
    else:
        rep.line("WOULD", f"remove {target}")


def step_d(home: Path, legacy: Path, apply: bool, rep: Report) -> None:
    print(f"[d] move {BUNDLE_NAME} into {legacy}")
    src = home / BUNDLE_NAME
    dst = legacy / BUNDLE_NAME
    if not (src.exists() or src.is_symlink()):
        rep.line("SKIP", "already absent")
        return
    if dst.exists() or dst.is_symlink():
        rep.fail(f"{dst} already exists; not overwriting (move {src} by hand)")
        return
    if apply:
        legacy.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        rep.line("OK", f"moved {src} -> {dst}")
    else:
        rep.line("WOULD", f"move {src} -> {dst}")


def step_e(hermes: Path | None, apply: bool, rep: Report) -> None:
    print("[e] syntax-check restored files with the Hermes venv")
    if hermes is None:
        rep.line("SKIP", "no Hermes checkout")
        return
    py = hermes / "venv" / "bin" / "python"
    if not py.exists():
        rep.line("SKIP", f"no venv at {py}")
        return
    present = [f for f in PATCHED_FILES if (hermes / f).is_file()]
    if not present:
        rep.line("SKIP", "no patched files present")
        return
    if not apply:
        rep.line("WOULD", f"compile {len(present)} file(s) with {py}")
        return
    r = subprocess.run(
        [str(py), "-c", _COMPILE_SNIPPET, *present],
        cwd=str(hermes),
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode == 0:
        rep.line("OK", f"{len(present)} file(s) compile")
    else:
        rep.fail("syntax errors: " + (r.stdout + r.stderr).strip())


def post_check(hermes: Path | None, apply: bool, rep: Report) -> None:
    print("[post] residual devin_acp / devin-acp references in tracked Hermes files")
    if hermes is None:
        rep.line("SKIP", "no verified git checkout")
        return
    r = git(hermes, "grep", "-l", "-E", REF_PATTERN, "--", ".")
    if r.returncode == 1:
        rep.line("OK", "no references")
        return
    if r.returncode != 0:
        raise RuntimeError(f"git grep failed: {r.stderr.strip()}")
    hits = r.stdout.split()
    shown = ", ".join(hits[:20]) + (" ..." if len(hits) > 20 else "")
    if apply:
        rep.fail(f"{len(hits)} file(s) still reference devin_acp/devin-acp: {shown}")
    else:
        rep.line("INFO", f"{len(hits)} file(s) reference devin_acp/devin-acp before apply: {shown}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Remove the old Hermes devin-acp integration (dry run unless --apply)."
    )
    ap.add_argument("--apply", action="store_true", help="perform the changes (default: dry run)")
    ap.add_argument(
        "--hermes-home",
        type=Path,
        help="Hermes home (default: $HERMES_HOME or ~/.hermes)",
    )
    ap.add_argument(
        "--legacy-dir",
        type=Path,
        default=REPO_ROOT / "legacy",
        help="destination for the reuse bundle (default: <repo>/legacy)",
    )
    args = ap.parse_args(argv)

    home = (args.hermes_home or Path(os.environ.get("HERMES_HOME") or "~/.hermes")).expanduser()
    hermes = home / "hermes-agent"
    rep = Report()
    mode = "APPLY" if args.apply else "DRY RUN (no changes; pass --apply to perform)"
    print(f"remove_devin_acp: {mode}")
    print(f"hermes home: {home}")
    try:
        checkout: Path | None = None
        if not hermes.is_dir():
            print(f"hermes checkout: not found at {hermes} (git steps skipped)")
        elif is_own_checkout(hermes, rep):
            checkout = hermes
        present_dir: Path | None = hermes if hermes.is_dir() else None

        step_a(checkout, args.apply, rep)
        step_b(checkout, args.apply, rep)
        step_c(home, args.apply, rep)
        step_d(home, args.legacy_dir, args.apply, rep)
        step_e(present_dir, args.apply, rep)
        post_check(checkout, args.apply, rep)
    except (RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print()
    if not args.apply:
        if rep.attention:
            print(f"dry run: {rep.attention} item(s) would block --apply (see FAIL lines). Nothing changed.")
        else:
            print("dry run complete. Nothing changed; re-run with --apply to perform the WOULD steps.")
    elif rep.attention:
        print(f"cutover finished with {rep.attention} item(s) needing attention (see FAIL lines).")
    else:
        print("cutover complete.")
    return 2 if rep.attention else 0


if __name__ == "__main__":
    sys.exit(main())
