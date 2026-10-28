#!/usr/bin/env python3
"""Format and check the Markdown notes in this repo.

Fixes (safe, mechanical):
  - CRLF / CR -> LF
  - trailing whitespace: a single trailing space is dropped, two or more
    collapse to exactly two (two trailing spaces are a Markdown hard break
    and are kept on purpose), inside code fences all trailing whitespace goes
  - exactly one final newline, no trailing blank lines
  - three or more consecutive blank lines collapse to one (outside fences)
  - info string of a fence loses surrounding whitespace

Reports (never auto-fixed):
  - fenced code block with no language tag
  - decorative `---` horizontal rule (forbidden by AGENTS.md)
  - emoji (forbidden by AGENTS.md)
  - relative link that does not resolve
  - heading level that skips a step (# -> ###)

Usage:
  python3 scripts/format.py            # fix in place
  python3 scripts/format.py --check    # report only, exit 1 on errors
  python3 scripts/format.py --check --strict   # warnings also fail
  python3 scripts/format.py --quiet    # summary only, warnings suppressed
  python3 scripts/format.py cxx go     # limit to paths
"""

from __future__ import annotations

import difflib
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", "node_modules", ".venv", ".build", "dist", "out"}
FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")
HEADING_RE = re.compile(r"^(#{1,6})\s+\S")
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)")
# Decorative emoji only. Typographic marks such as U+2713 (check) and
# U+2717 (cross) are allowed: they are used as list markers in a few notes.
EMOJI_RE = re.compile("[\U0001f000-\U0001faff\u2705\u274c\u2757\u26a0\ufe0f]")

# Rules that block a commit; everything else is reported but allowed.
ERROR_KINDS = {"fence", "link"}


def note_files(paths):
    if paths:
        for p in paths:
            full = p if os.path.isabs(p) else os.path.join(os.getcwd(), p)
            if os.path.isdir(full):
                for dirpath, dirnames, filenames in os.walk(full):
                    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                    for name in sorted(filenames):
                        if name.endswith(".md"):
                            yield os.path.join(dirpath, name)
            elif full.endswith(".md"):
                yield full
        return
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            if name.endswith(".md"):
                yield os.path.join(dirpath, name)


def split_lines(text):
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def tidy_trailing(line, in_fence):
    if in_fence:
        return line.rstrip(" \t")
    stripped = line.rstrip(" \t")
    if stripped == line:
        return line
    # A deliberate Markdown hard break is exactly two trailing spaces.
    if line.endswith("  "):
        return stripped + "  "
    return stripped


def fix(text):
    lines = split_lines(text)
    out = []
    in_fence = False
    for line in lines:
        m = FENCE_RE.match(line)
        if m:
            indent, marker, info = m.groups()
            out.append(indent + marker + info.strip())
            in_fence = not in_fence
            continue
        out.append(tidy_trailing(line, in_fence))

    # Collapse runs of blank lines outside fences.
    collapsed = []
    in_fence = False
    blanks = 0
    for line in out:
        if FENCE_RE.match(line):
            in_fence = not in_fence
            blanks = 0
            collapsed.append(line)
            continue
        if not in_fence and line == "":
            blanks += 1
            if blanks > 1:
                continue
        else:
            blanks = 0
        collapsed.append(line)

    while collapsed and collapsed[-1] == "":
        collapsed.pop()
    return "\n".join(collapsed) + "\n"


def check(path, text):
    findings = []
    in_fence = False
    prev_heading = 0
    for number, line in enumerate(text.split("\n"), start=1):
        m = FENCE_RE.match(line)
        if m:
            info = m.group(3)
            if not in_fence:
                in_fence = True
                if not info.strip():
                    findings.append(
                        ("fence", number, "code fence without a language tag")
                    )
            else:
                in_fence = False
            continue
        if in_fence:
            continue
        if line == "---":
            findings.append(("hr", number, "decorative `---` (AGENTS.md forbids it)"))
        if EMOJI_RE.search(line):
            findings.append(("emoji", number, "emoji in note body"))
        h = HEADING_RE.match(line)
        if h:
            level = len(h.group(1))
            if prev_heading and level > prev_heading + 1:
                findings.append(
                    ("heading", number, f"heading level jumps {prev_heading} -> {level}")
                )
            prev_heading = level
        if "`" in line:
            scan = re.sub(r"`[^`]*`", "", line)
        else:
            scan = line
        for target in LINK_RE.findall(scan):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            path_part = target.split("#", 1)[0]
            if not path_part:
                continue
            resolved = os.path.normpath(os.path.join(os.path.dirname(path), path_part))
            if not os.path.exists(resolved):
                findings.append(("link", number, f"link target missing: {path_part}"))
    return findings


def display(path):
    rel = os.path.relpath(path, ROOT)
    return path if rel.startswith("..") else rel


def main(argv):
    check_only = "--check" in argv
    strict = "--strict" in argv
    quiet = "--quiet" in argv
    paths = [a for a in argv if not a.startswith("-")]
    known = {"--check", "--strict", "--quiet"}
    unknown = [a for a in argv if a.startswith("-") and a not in known]
    if unknown:
        print("unknown option: " + ", ".join(unknown), file=sys.stderr)
        return 2

    files = sorted(note_files(paths))
    if not files:
        print("no markdown files found", file=sys.stderr)
        return 2

    errors = 0
    warnings = 0
    touched = []
    for path in files:
        with open(path, encoding="utf-8") as fh:
            original = fh.read()
        if not check_only:
            fixed = fix(original)
            if fixed != original:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(fixed)
                matcher = difflib.SequenceMatcher(
                    None, original.split("\n"), fixed.split("\n")
                )
                delta = sum(
                    max(i2 - i1, j2 - j1)
                    for tag, i1, i2, j1, j2 in matcher.get_opcodes()
                    if tag != "equal"
                )
                touched.append((display(path), delta))
            original = fixed
        for kind, number, message in check(path, original):
            blocking = kind in ERROR_KINDS
            if blocking:
                errors += 1
            else:
                warnings += 1
                if quiet:
                    continue
            print(f"{display(path)}:{number}: {'error' if blocking else 'warning'}: {message}")

    for rel, count in touched:
        print(f"fixed {rel} ({count} line(s))")
    print(
        f"{len(files)} file(s), {len(touched)} rewritten, "
        f"{errors} error(s), {warnings} warning(s)"
    )
    if errors:
        return 1
    if strict and warnings:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
