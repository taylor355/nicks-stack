#!/usr/bin/env python3
# ==========================================================================
# Taylor AI Platform — skill index pruning  (v1.1.15)
# ==========================================================================
# Every installed skill contributes ~65-90 bytes to an always-on index that is
# billed on EVERY call to EVERY model, whether or not the skill is ever used.
# Measured with `hermes prompt-size`: 105 skills = 8,803 B (~2,200 tokens).
#
# WHY NOT `hermes skills opt-out --remove`
#   It is all-or-nothing over BUNDLED skills. This platform's own 23 skills are
#   setup/infrastructure ones; the genuinely useful day-to-day skills (notion,
#   docx, pdf, xlsx, obsidian, github, claude-code, codex) are bundled. So the
#   blunt switch would delete exactly the ones worth keeping. What it IS good
#   for is the marker: `opt-out` without `--remove` writes .no-bundled-skills,
#   which stops `hermes update` re-seeding whatever we prune here.
#
# So: set the marker (declared), then remove exactly the named skills.
#
#   skills_prune.py apply    prune per platform.yaml, print what changed
#   skills_prune.py status   report only; removes nothing
#
# Idempotent. A name already absent is not an error — that is the steady state
# after the first run. Never touches a skill that is not named.
# ==========================================================================
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

MARKER_NAME = ".no-bundled-skills"


def spec() -> dict:
    block = lib.load_yaml(lib.PLATFORM_FILE).get("skills")
    if not isinstance(block, dict):
        return {"prune": [], "prune_bundled": False}
    names = [str(n).strip() for n in (block.get("prune") or []) if str(n).strip()]
    return {"prune": names, "prune_bundled": bool(block.get("prune_bundled"))}


def skills_root() -> Path:
    return lib.HERMES_HOME / "skills"


def _safe_target(root: Path, name: str) -> Path | None:
    """Resolve 'category/skill' under the skills root, refusing to escape it.

    A declared name is data from a config file, so it is treated as untrusted:
    '..' or an absolute path must not be able to delete something outside the
    skills tree.
    """
    if name.startswith("/") or ".." in Path(name).parts:
        return None
    target = (root / name).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return None
    return target


def apply(dry_run: bool = False) -> int:
    cfg = spec()
    root = skills_root()
    if not root.is_dir():
        print(f"skills: {root} does not exist — nothing to prune")
        return 0

    before = len(list(root.glob("*/*/SKILL.md")))
    removed, missing, refused = [], [], []

    for name in cfg["prune"]:
        target = _safe_target(root, name)
        if target is None:
            refused.append(name)
            continue
        if not target.is_dir():
            missing.append(name)
            continue
        if dry_run:
            removed.append(name)
            continue
        shutil.rmtree(target, ignore_errors=True)
        removed.append(name)

    # Drop category directories left empty by the prune, so the tree stays
    # tidy and `ls skills/` reflects what the agent actually has.
    if not dry_run:
        for cat in sorted(p for p in root.iterdir() if p.is_dir()):
            if not any(cat.iterdir()):
                cat.rmdir()

    marker = root / MARKER_NAME
    marker_action = "already set" if marker.exists() else "not set"
    if cfg["prune_bundled"] and not marker.exists() and not dry_run:
        # Prefer the CLI so Hermes owns the marker's semantics; fall back to
        # writing it directly if the subcommand is unavailable in this build.
        try:
            subprocess.run(["hermes", "skills", "opt-out"],
                           capture_output=True, text=True, timeout=120, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
        if not marker.exists():
            try:
                marker.write_text(
                    "# Written by nicks-stack skills_prune (platform.yaml skills.prune).\n"
                    "# Stops `hermes update` re-seeding bundled skills that were pruned.\n"
                    "# Undo with: sudo hermes skills opt-in --sync\n")
            except OSError as exc:
                print(f"skills: could not write {marker}: {exc}", file=sys.stderr)
        marker_action = "set" if marker.exists() else "FAILED to set"

    after = len(list(root.glob("*/*/SKILL.md")))
    verb = "would remove" if dry_run else "removed"
    print(f"skills: {verb} {len(removed)}, already absent {len(missing)}, "
          f"{before} -> {after} skills")
    if refused:
        print(f"skills: REFUSED unsafe name(s): {', '.join(refused)}", file=sys.stderr)
    print(f"skills: re-seed marker {marker_action}")
    return 1 if refused else 0


def status() -> int:
    cfg = spec()
    root = skills_root()
    present = sorted(p.parent.relative_to(root).as_posix()
                     for p in root.glob("*/*/SKILL.md")) if root.is_dir() else []
    still_here = [n for n in cfg["prune"] if n in present]
    print("Skill index")
    print("────────────────────")
    print(f"  installed      {len(present)}")
    print(f"  declared prune {len(cfg['prune'])}")
    print(f"  still present  {len(still_here)}"
          + ("  (run: sudo jack skills prune)" if still_here else ""))
    print(f"  re-seed marker {'set' if (root / MARKER_NAME).exists() else 'NOT set'}")
    for n in still_here:
        print(f"    ! {n}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Prune the always-on skill index")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("apply", help="remove the declared skills")
    p.add_argument("--dry-run", action="store_true", help="report, change nothing")
    sub.add_parser("status", help="report only")
    args = ap.parse_args()
    return apply(dry_run=args.dry_run) if args.cmd == "apply" else status()


if __name__ == "__main__":
    sys.exit(main())
