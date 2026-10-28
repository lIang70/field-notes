"""Build-time generation of per-directory landing pages.

Every directory that holds notes gets a generated ``index.md`` that renders a
card grid over its children (title + one-line blurb pulled from the note's own
``## TL;DR``). Nothing is written into git, so a landing page can never go
stale: drop ``ai/harness/foo.md`` in and the ``ai/harness/`` page lists it on
the next build.

Wired from ``mkdocs.yml`` via ``hooks: [hooks/area_index.py]``.
"""

from __future__ import annotations

import html
import re
from collections import defaultdict
from pathlib import PurePosixPath

from mkdocs.structure.files import File
from mkdocs.structure.nav import Section

TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
TLDR_RE = re.compile(r"^##\s*TL;DR\s*$", re.MULTILINE | re.IGNORECASE)
LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
TAG_RE = re.compile(r"<[^>]+>")
MARK_RE = re.compile(r"[`*_]{1,2}")
SKIP_FIRST = set("#>|-*<!:")
INDEX_STEMS = {"README", "index"}

MAX_BLURB = 92


def _parent_dir(src_uri: str) -> str:
    parent = str(PurePosixPath(src_uri).parent)
    return "" if parent == "." else parent


def _parent(directory: str) -> str:
    parent = str(PurePosixPath(directory).parent)
    return "" if parent == "." else parent


def _clean(text: str) -> str:
    text = LINK_RE.sub(r"\1", text)
    text = TAG_RE.sub("", text)
    text = MARK_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _summarize(raw: str, fallback: str) -> tuple[str, str]:
    """Return ``(title, one-line blurb)`` for a note's raw markdown."""
    title = ""
    match = TITLE_RE.search(raw)
    if match:
        title = _clean(match.group(1))
    body = raw[match.end() :] if match else raw

    blurb = ""
    tldr = TLDR_RE.search(body)
    if tldr:
        for line in body[tldr.end() :].lstrip().splitlines():
            line = line.strip()
            if line.startswith(("- ", "* ")):
                blurb = line[2:]
                break
            if line.startswith("#"):  # empty TL;DR section
                break
    if not blurb:
        for line in body.splitlines():
            line = line.strip()
            if not line or line[0] in SKIP_FIRST:
                continue
            blurb = line
            break

    blurb = _clean(blurb)
    if len(blurb) > MAX_BLURB:
        blurb = blurb[:MAX_BLURB].rstrip(" ,，。;；、") + "…"
    return title or fallback, blurb


def _read(path: str | None) -> str:
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def _render_page(
    label: str,
    kicker: str,
    lede: str,
    cards: list[tuple[str, str, str]],
    subdirs: list[tuple[str, str, int, str]],
) -> str:
    # `title` in front matter drives the nav label + <title>; the visible header
    # is hand-built so the kicker can sit above the title.
    out = [
        "---",
        f"title: {label}",
        "hide:",
        "  - toc",
        "---",
        "",
        '<header class="area-head">',
    ]
    if kicker:
        out.append(f'<p class="area-kicker">{html.escape(kicker)}</p>')
    out.append(f'<h1 class="area-title">{html.escape(label)}</h1>')
    if lede:
        out.append(f'<p class="area-lede">{html.escape(lede)}</p>')
    out.extend(["</header>", ""])

    if cards and subdirs:
        # a group strip above the note cards
        out.append('<div class="sub-grid">')
        for sub_label, sub_href, sub_count, _ in subdirs:
            out.append(
                f'<a class="sub-card" href="{html.escape(sub_href)}">'
                f'<span class="sub-card__title">{html.escape(sub_label)}</span>'
                f'<span class="sub-card__meta">{sub_count} 篇 · 进入分组</span>'
                f"</a>"
            )
        out.append("</div>")
        out.append("")
    elif subdirs:
        # directory holding only subdirectories (go/): promote the groups to
        # the primary card grid and preview what is inside each one
        cards = [
            (sub_label, f"{sub_count} 篇 · {sub_blurb}", sub_href)
            for sub_label, sub_href, sub_count, sub_blurb in subdirs
        ]

    if cards:
        out.append('<div class="note-grid">')
        for title, blurb, href in cards:
            out.append(
                f'<a class="note-card" href="{html.escape(href)}">'
                f'<span class="note-card__title">{html.escape(title)}</span>'
                f'<span class="note-card__blurb">{html.escape(blurb)}</span>'
                f"</a>"
            )
        out.append("</div>")
        out.append("")
    return "\n".join(out)


def on_nav(nav, config, files):  # noqa: ANN001 - mkdocs hook signature
    """Relabel auto-generated sections: folder `cxx/` -> "C++", `ai/` -> "AI"."""
    areas = {a["dir"].strip("/"): a for a in config.setdefault("extra", {}).get("areas", [])}

    def walk(items):
        for item in items:
            children = getattr(item, "children", None)
            if not children:
                continue
            head = children[0]
            if isinstance(item, Section) and getattr(head, "is_page", False):
                label = areas.get(_parent_dir(head.file.src_uri), {}).get("label")
                if label:
                    item.title = label
            walk(children)

    walk(nav.items)
    if nav.homepage is not None:
        nav.homepage.title = "首页"
    return nav


def on_files(files, config):  # noqa: ANN001 - mkdocs hook signature
    extra = config.setdefault("extra", {})
    areas = {a["dir"].strip("/"): a for a in extra.get("areas", [])}

    notes: dict[str, list[File]] = defaultdict(list)
    dirs: set[str] = {""}
    has_index: set[str] = set()
    for f in files:
        if not f.src_uri.endswith(".md"):
            continue
        path = PurePosixPath(f.src_uri)
        directory = "" if path.parent == PurePosixPath(".") else str(path.parent)
        # a directory may hold only subdirectories (go/), so register every
        # ancestor, not just the file's own parent
        for ancestor in path.parents:
            dirs.add("" if ancestor == PurePosixPath(".") else str(ancestor))
        if path.stem in INDEX_STEMS:
            has_index.add(directory)
        else:
            notes[directory].append(f)

    children: dict[str, set[str]] = defaultdict(set)
    for directory in dirs:
        if directory:
            children[_parent(directory)].add(directory)

    def subtree_notes(directory: str) -> int:
        return len(notes.get(directory, [])) + sum(
            subtree_notes(child) for child in children.get(directory, ())
        )

    generated: list[File] = []
    for directory in sorted(d for d in dirs if d):
        if directory in has_index:
            continue
        meta = areas.get(directory, {})
        cards: list[tuple[str, str, str]] = []
        for f in sorted(notes.get(directory, []), key=lambda item: item.src_uri.lower()):
            stem = PurePosixPath(f.src_uri).stem
            title, blurb = _summarize(_read(f.abs_src_path), stem)
            cards.append((title, blurb, f"{stem}/"))
        subdirs = []
        for child in sorted(children.get(directory, ())):
            child_notes = sorted(notes.get(child, []), key=lambda item: item.src_uri.lower())
            titles = [
                _summarize(_read(f.abs_src_path), PurePosixPath(f.src_uri).stem)[0]
                for f in child_notes
            ]
            subdirs.append(
                (
                    areas.get(child, {}).get("label", child.split("/")[-1].title()),
                    f"{PurePosixPath(child).name}/",
                    subtree_notes(child),
                    "、".join(titles[:8]),
                )
            )
        if not cards and not subdirs:
            continue
        content = _render_page(
            label=meta.get("label", directory.split("/")[-1].title()),
            kicker=meta.get("kicker", ""),
            lede=meta.get("tagline", ""),
            cards=cards,
            subdirs=subdirs,
        )
        generated.append(File.generated(config, f"{directory}/index.md", content=content))

    for directory, meta in areas.items():
        meta["href"] = f"{directory}/"
        meta["count"] = subtree_notes(directory)

    extra["stats"] = {
        "notes": sum(len(items) for items in notes.values()),
        "areas": len([d for d in areas if "/" not in d]),
    }
    for f in generated:
        files.append(f)
    return files
