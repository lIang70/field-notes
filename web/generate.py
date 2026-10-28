"""Generate the site's navigation scaffolding from the notes tree.

Zensical (and MkDocs) cannot derive two things from a plain Markdown tree the
way this site wants them: a card-grid landing page per directory, and section
labels that read "AI" instead of "Ai". MkDocs solved that with a `hooks:`
entry, but **Zensical does not support hooks**, so the same work happens here,
in the staging step: `web/build.py` copies the notes into `web/.build/docs`,
calls :func:`generate` to write the landing pages into that staging directory,
and :func:`nav_yaml` to emit an explicit `nav` block for the generated config.

Nothing is written into git, so a landing page can never go stale: drop
`ai/harness/foo.md` in and it shows up on the next build.
"""

from __future__ import annotations

import html
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class Area:
    """A top-level grouping, used for the home-page cards and as nav label."""

    path: str
    label: str
    kicker: str
    tagline: str


AREAS: tuple[Area, ...] = (
    Area(
        "go",
        "Go",
        "01 / 语言与运行时",
        "分层与插件宿主、工程规范与发布、构建打包、并发、语言机制、运行时",
    ),
    Area(
        "rust",
        "Rust",
        "02 / 所有权与边界",
        "所有权与 trait、内部可变性、unsafe 边界、内存布局与分配、tokio 与 async/blocking",
    ),
    Area(
        "os",
        "OS",
        "03 / 系统内部",
        "进程与调度、内存、并发原语、I/O、文件系统、虚拟化、网络栈",
    ),
    Area(
        "ai",
        "AI",
        "04 / 智能体工程",
        "agent、上下文工程、记忆、RAG、工具与 MCP、多智能体、评测",
    ),
    Area(
        "ai/harness",
        "AI Harness",
        "04b / 运行时层",
        "沙箱、控制循环、重试与超时、状态持久化、可观测、I/O、生命周期",
    ),
)
AREA_BY_PATH = {area.path: area for area in AREAS}
LABELS = {area.path: area.label for area in AREAS}
INDEX_NAMES = {"index.md", "README.md"}

TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
TLDR_RE = re.compile(r"^##\s*TL;DR\s*$", re.MULTILINE | re.IGNORECASE)
LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
TAG_RE = re.compile(r"<[^>]+>")
MARK_RE = re.compile(r"[`*_]{1,2}")
SKIP_FIRST = set("#>|-*<!:")
MAX_BLURB = 92


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


def _root_of(rel: str) -> str:
    return rel.split("/", 1)[0]


def _label(directory: str) -> str:
    if directory in LABELS:
        return LABELS[directory]
    return PurePosixPath(directory).name.replace("_", " ").title()


def _scan(docs: Path) -> tuple[dict[str, list[tuple[str, str, str]]], set[str], set[str]]:
    """Walk the staged docs.

    Returns ``(notes_by_dir, dirs, dirs_with_index)`` where ``notes_by_dir``
    maps a directory to ``(stem, title, blurb)`` for its own pages.
    """
    notes: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    dirs: set[str] = {""}
    has_index: set[str] = set()
    for path in sorted(docs.rglob("*.md")):
        rel = path.relative_to(docs).as_posix()
        directory = str(PurePosixPath(rel).parent)
        directory = "" if directory == "." else directory
        for ancestor in PurePosixPath(rel).parents:
            dirs.add("" if ancestor == PurePosixPath(".") else str(ancestor))
        if PurePosixPath(rel).name in INDEX_NAMES:
            has_index.add(directory)
            continue
        title, blurb = _summarize(path.read_text(encoding="utf-8"), PurePosixPath(rel).stem)
        notes[directory].append((PurePosixPath(rel).stem, title, blurb))
    return notes, dirs, has_index


def _children(dirs: set[str]) -> dict[str, set[str]]:
    children: dict[str, set[str]] = defaultdict(set)
    for directory in dirs:
        if directory:
            parent = str(PurePosixPath(directory).parent)
            children["" if parent == "." else parent].add(directory)
    return children


def _count(dir_path: str, notes, children) -> int:
    return len(notes.get(dir_path, [])) + sum(
        _count(child, notes, children) for child in children.get(dir_path, ())
    )


def _area_page(directory: str, notes, children) -> str:
    meta = AREA_BY_PATH.get(directory)
    cards = [
        (title, blurb, f"{stem}/")
        for stem, title, blurb in sorted(notes.get(directory, []), key=lambda item: item[0].lower())
    ]
    subdirs = []
    for child in sorted(children.get(directory, ())):
        titles = [title for _, title, _ in notes.get(child, [])]
        subdirs.append(
            (
                _label(child),
                f"{PurePosixPath(child).name}/",
                _count(child, notes, children),
                "、".join(titles[:8]),
            )
        )

    out = [
        "---",
        f"title: {json.dumps(_label(directory), ensure_ascii=False)}",
        "hide:",
        "  - toc",
        "---",
        "",
        '<header class="area-head">',
    ]
    if meta:
        out.append(f'<p class="area-kicker">{html.escape(meta.kicker)}</p>')
    out.append(f'<h1 class="area-title">{html.escape(_label(directory))}</h1>')
    if meta:
        out.append(f'<p class="area-lede">{html.escape(meta.tagline)}</p>')
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
        # a directory that holds only subdirectories: promote the groups to the
        # primary grid and preview what is inside each one
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


def _home_page(notes, children) -> str:
    total = sum(len(items) for items in notes.values())
    areas = [area for area in AREAS if "/" not in area.path]
    cards = []
    for area in AREAS:
        is_sub = "/" in area.path
        cards.append(
            f'<a class="area-card{" area-card--sub" if is_sub else ""}" href="{area.path}/">'
            f'<span class="area-card__kicker">{html.escape(area.kicker)}</span>'
            f'<span class="area-card__label">{html.escape(area.label)}</span>'
            f'<span class="area-card__tagline">{html.escape(area.tagline)}</span>'
            f'<span class="area-card__count">{_count(area.path, notes, children)}<i>篇</i></span>'
            f"</a>"
        )

    return "\n".join(
        [
            "---",
            'title: "首页"',
            "hide:",
            "  - toc",
            "---",
            "",
            '<div class="home">',
            "  <section class=\"hero\">",
            '    <p class="hero__kicker"><span class="hero__dot"></span>personal engineering notes</p>',
            '    <h1 class="hero__title">工程笔记<span class="hero__title-en">field notes</span></h1>',
            '    <p class="hero__lede">',
            "      写给未来的自己：只留<strong>反直觉的点</strong>和<strong>工程的抓手</strong>，",
            "      不抄教科书、不覆盖从零开始。<br>",
            "      读完应该能在三分钟内回忆起“当时为什么这么选”。",
            "    </p>",
            '    <dl class="hero__stats">',
            f'      <div><dt>notes</dt><dd>{total}</dd></div>',
            f'      <div><dt>areas</dt><dd>{len(areas)}</dd></div>',
            '      <div><dt>license</dt><dd class="hero__stat-text">CC-BY-4.0</dd></div>',
            '      <div><dt>source</dt><dd class="hero__stat-text">'
            '<a href="https://github.com/lIang70/field-notes">GitHub</a></dd></div>',
            "    </dl>",
            "  </section>",
            "",
            '  <section class="home__areas" aria-label="areas">',
            *cards,
            "  </section>",
            "",
            '  <section class="home__cols">',
            "    <article>",
            "      <h3>这是什么</h3>",
            "      <p>个人笔记本。断言、取舍、踩过的坑，以及在两个方案之间选一个的理由。</p>",
            "    </article>",
            "    <article>",
            "      <h3>这不是什么</h3>",
            "      <p>不是教程、不是参考手册。假设你已经会写代码，也不保证全部正确。</p>",
            "    </article>",
            "    <article>",
            "      <h3>怎么用</h3>",
            '      <p>按区域进卡片页翻，或用 <kbd>/</kbd> 搜索；仓库里也能直接 '
            '<a href="https://github.com/lIang70/field-notes">ripgrep</a>。</p>',
            "    </article>",
            "  </section>",
            "",
            '  <p class="home__foot">',
            '    内容以 <a href="https://creativecommons.org/licenses/by/4.0/">CC-BY-4.0</a> 授权，',
            "    转载请注明出处。笔记里的图（mermaid）与代码块可直接复制。",
            "  </p>",
            "</div>",
            "",
        ]
    )


def generate(docs: Path) -> dict[str, int]:
    """Write the landing pages into ``docs`` and return build stats."""
    notes, dirs, has_index = _scan(docs)
    children = _children(dirs)

    (docs / "index.md").write_text(_home_page(notes, children), encoding="utf-8")

    for directory in sorted(directory for directory in dirs if directory):
        if directory in has_index:
            continue
        if not notes.get(directory) and not children.get(directory):
            continue
        target = docs / directory / "index.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_area_page(directory, notes, children), encoding="utf-8")

    return {
        "notes": sum(len(items) for items in notes.values()),
        "areas": len([area for area in AREAS if "/" not in area.path]),
    }


def nav_yaml(docs: Path) -> str:
    """Emit an explicit ``nav`` block so section labels are not directory names."""
    notes, dirs, _ = _scan(docs)
    children = _children(dirs)

    def page(directory: str, stem: str) -> str:
        return f"{directory}/{stem}.md" if directory else f"{stem}.md"

    def block(directory: str, depth: int) -> list[str]:
        pad = "  " * depth
        lines: list[str] = []
        index = page(directory, "index")
        if (docs / index).exists():
            lines.append(f"{pad}- {index}")
        for stem, _, _ in sorted(notes.get(directory, []), key=lambda item: item[0].lower()):
            lines.append(f"{pad}- {page(directory, stem)}")
        for child in sorted(children.get(directory, ())):
            lines.append(f"{pad}- {json.dumps(_label(child), ensure_ascii=False)}:")
            lines.extend(block(child, depth + 1))
        return lines

    rows = ["nav:", '  - "首页": index.md']
    for child in sorted(children.get("", ()), key=lambda d: _nav_order(d)):
        rows.append(f'  - {json.dumps(_label(child), ensure_ascii=False)}:')
        rows.extend(block(child, 2))
    return "\n".join(rows) + "\n"


def _nav_order(directory: str) -> tuple[int, str]:
    order = {area.path: index for index, area in enumerate(AREAS)}
    return (order.get(directory, len(order)), directory)
