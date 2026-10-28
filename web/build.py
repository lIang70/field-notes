#!/usr/bin/env python3
"""Stage the notes into `web/.build/docs` and run MkDocs.

The notes live at the repo root so that GitHub's own Markdown view stays the
primary reading surface. MkDocs wants a *child* directory as `docs_dir`, so we
copy the Markdown tree (preserving paths, hence preserving relative links) into
`web/.build/docs` on every build. Nothing staged is committed: `.gitignore` is
in whitelist mode and only allows what we list there.

Everything web-related (config, hooks, templates, CSS, this script) lives under
`web/`; the only things left outside are `Makefile` (entry point) and
`.github/workflows/site.yml` (GitHub requires that path).

Usage:
    web/build.py            # build web/.build/site
    web/build.py --serve    # live reload on http://127.0.0.1:8000
    web/build.py --strict   # build, fail on warnings (link check)
    web/build.py --stats    # per-area markdown counts (README index)
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

WEB = Path(__file__).resolve().parent
ROOT = WEB.parent
BUILD = WEB / ".build"
DOCS = BUILD / "docs"
STAGED_DIRS = ("assets", "stylesheets")  # inside web/, copied next to the pages
STAGED_FILES = ("LICENSE",)  # README links to it
SKIP_META = {"AGENTS.md"}
SKIP_DIRS = {"web"}  # the site sources themselves are not pages


def _notes() -> list[Path]:
    found = []
    for path in ROOT.rglob("*.md"):
        rel = path.relative_to(ROOT)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if rel.parts[0] in SKIP_DIRS:
            continue
        if rel.name in SKIP_META:
            continue
        found.append(rel)
    return sorted(found)


def stage() -> int:
    if DOCS.exists():
        shutil.rmtree(DOCS)
    DOCS.mkdir(parents=True)
    files = _notes()
    for rel in files:
        dest = DOCS / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dest)
    for name in STAGED_FILES:
        if (ROOT / name).is_file():
            shutil.copy2(ROOT / name, DOCS / name)
    for name in STAGED_DIRS:
        src = WEB / name
        if src.is_dir():
            shutil.copytree(src, DOCS / name, dirs_exist_ok=True)
    print(f"staged {len(files)} markdown files -> {DOCS.relative_to(ROOT)}")
    return len(files)


def stats() -> None:
    counts: dict[str, int] = {}
    for rel in _notes():
        if rel.name in {"README.md"}:
            continue
        top = rel.parts[0] if len(rel.parts) > 1 else "(root)"
        counts[top] = counts.get(top, 0) + 1
    total = sum(counts.values())
    for area in sorted(counts):
        print(f"{area:<8} {counts[area]:>3}")
    print(f"{'total':<8} {total:>3}")


FLAGS = {"--serve", "--strict", "-s", "--stats"}


def _ensure_mkdocs() -> None:
    """Re-exec into .venv when the caller's interpreter has no MkDocs."""
    if importlib.util.find_spec("mkdocs") is not None:
        return
    venv = WEB / ".venv" / "bin" / "python"
    if venv.exists():
        os.execv(str(venv), [str(venv), *sys.argv])
    sys.exit("mkdocs is missing: run `make site` (it creates web/.venv, see web/requirements.txt)")


def main(argv: list[str]) -> int:
    if "--stats" in argv:
        stats()
        return 0
    _ensure_mkdocs()
    serve = "--serve" in argv
    strict = bool({"--strict", "-s"} & set(argv))
    rest = [arg for arg in argv if arg not in FLAGS]
    stage()
    command = [
        sys.executable,
        "-m",
        "mkdocs",
        "serve" if serve else "build",
        "-f",
        str(WEB / "mkdocs.yml"),
    ]
    if strict and not serve:
        command.append("--strict")
    return subprocess.call(command + rest, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
