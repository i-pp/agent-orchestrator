#!/usr/bin/env python3
"""Check local Markdown and HTML links without network access."""
from __future__ import annotations

import argparse
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
import re
import sys
import unicodedata
from urllib.parse import unquote, urlsplit

IGNORED_DIRS = {".git", "node_modules", ".runtime", "dist", "build", "__pycache__"}
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
REFERENCE = re.compile(r"^ {0,3}\[[^\]]+\]:\s*(?:<([^>]+)>|(\S+))")
HEADING = re.compile(r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$")
EXPLICIT_ID = re.compile(r"\b(?:id|name)\s*=\s*(['\"])(.*?)\1", re.IGNORECASE)
HTML_TAG = re.compile(r"<\s*[A-Za-z][^>]*>")


def remove_code_and_comments(source: str) -> str:
    lines = []
    fence_char = None
    fence_len = 0
    for line in source.splitlines():
        match = FENCE.match(line)
        if fence_char is None and match:
            fence_char, fence_len = match.group(1)[0], len(match.group(1))
            lines.append("")
            continue
        if fence_char is not None:
            marker = re.match(r"^ {0,3}(" + re.escape(fence_char) + r"{" + str(fence_len) + r",})\s*$", line)
            if marker:
                fence_char = None
                fence_len = 0
            lines.append("")
            continue
        lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    # Remove inline code spans so examples are not treated as navigation.
    text = re.sub(r"(?<!\\)(`+).*?\1", "", text)
    return text


def find_bracket_end(text: str, start: int) -> int | None:
    depth = 0
    i = start
    while i < len(text):
        char = text[i]
        if char == "\\":
            i += 2
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def parse_inline_destination(text: str, start: int) -> tuple[str | None, int]:
    i = start + 1
    while i < len(text) and text[i].isspace():
        i += 1
    if i >= len(text):
        return None, i
    if text[i] == "<":
        j = i + 1
        while j < len(text):
            if text[j] == "\\":
                j += 2
                continue
            if text[j] == ">":
                return text[i + 1:j], j + 1
            j += 1
        return None, j
    begin = i
    depth = 0
    while i < len(text):
        char = text[i]
        if char == "\\":
            i += 2
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                return text[begin:i], i
            depth -= 1
        elif char.isspace() and depth == 0:
            return text[begin:i], i
        i += 1
    return text[begin:i], i


def markdown_links(source: str):
    text = remove_code_and_comments(source)
    for line_no, line in enumerate(text.splitlines(), 1):
        match = REFERENCE.match(line)
        if match:
            yield unescape(match.group(1) or match.group(2)), line_no
    i = 0
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] != "[":
            i += 1
            continue
        end = find_bracket_end(text, i)
        if end is None:
            i += 1
            continue
        j = end + 1
        while j < len(text) and text[j].isspace():
            j += 1
        if j < len(text) and text[j] == "(":
            destination, _ = parse_inline_destination(text, j)
            if destination is not None:
                line_no = text.count("\n", 0, i) + 1
                yield unescape(destination), line_no
        i = end + 1


class HtmlLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        for key in ("href", "src"):
            value = attributes.get(key)
            if value:
                self.links.append(unescape(value))
        for key in ("id", "name"):
            value = attributes.get(key)
            if value:
                self.ids.add(value)


def heading_slugs(source: str) -> set[str]:
    text = remove_code_and_comments(source)
    counts: dict[str, int] = {}
    slugs = set()
    for line in text.splitlines():
        match = HEADING.match(line)
        if not match:
            continue
        heading = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", match.group(1))
        heading = re.sub(r"<[^>]*>", "", heading)
        heading = heading.replace("`", "")
        chars = []
        for char in heading.lower():
            if char.isspace():
                chars.append("-")
            elif char == "-" or unicodedata.category(char)[0] in {"L", "N", "M"}:
                chars.append(char)
        slug = re.sub(r"-+", "-", "".join(chars)).strip("-")
        if not slug:
            continue
        count = counts.get(slug, 0)
        counts[slug] = count + 1
        slugs.add(slug if count == 0 else f"{slug}-{count}")
    return slugs


def markdown_files(root: Path, excludes: list[str]) -> list[Path]:
    result = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".md", ".markdown"}:
            continue
        rel = path.relative_to(root).as_posix()
        if any(part in IGNORED_DIRS for part in Path(rel).parts):
            continue
        if any(Path(rel).match(pattern) for pattern in excludes):
            continue
        result.append(path)
    return sorted(result)


def validate(root: Path, files: list[Path]) -> list[str]:
    failures = []
    root = root.resolve()
    cache: dict[Path, tuple[str, set[str]]] = {}
    for source in files:
        rel_source = source.relative_to(root).as_posix()
        raw = source.read_text(encoding="utf-8")
        parser = HtmlLinks()
        parser.feed(raw)
        links = list(markdown_links(raw)) + [(href, 1) for href in parser.links]
        for destination, line in links:
            if not destination or destination.startswith("#"):
                path = source
                fragment = unquote(destination[1:]) if destination.startswith("#") else ""
            else:
                parts = urlsplit(destination)
                if parts.scheme or parts.netloc or destination.startswith("//"):
                    continue
                decoded_path = unquote(parts.path)
                fragment = unquote(parts.fragment)
                path = (root / decoded_path.lstrip("/")) if decoded_path.startswith("/") else (source.parent / decoded_path)
                try:
                    path = path.resolve(strict=False)
                    path.relative_to(root)
                except ValueError:
                    failures.append(f"{rel_source}:{line}: link escapes repository: {destination}")
                    continue
            if not path.exists() and not path.suffix:
                for extension in (".md", ".markdown", ".html", ".htm"):
                    candidate = Path(str(path) + extension)
                    if candidate.is_file():
                        path = candidate
                        break
            if not path.exists():
                failures.append(f"{rel_source}:{line}: missing local target: {destination}")
                continue
            if path.is_dir():
                if fragment:
                    failures.append(f"{rel_source}:{line}: fragment on directory target: {destination}")
                continue
            if fragment and path.suffix.lower() in {".md", ".markdown", ".html", ".htm"}:
                if path not in cache:
                    target = path.read_text(encoding="utf-8", errors="replace")
                    html_parser = HtmlLinks()
                    html_parser.feed(target)
                    cache[path] = (target, heading_slugs(target) | html_parser.ids)
                _, anchors = cache[path]
                if fragment not in anchors:
                    failures.append(f"{rel_source}:{line}: missing fragment #{fragment} in {path.relative_to(root).as_posix()}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--exclude", action="append", default=[], help="repository-relative glob to skip")
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_dir():
        parser.error(f"repository root is not a directory: {root}")
    files = markdown_files(root, args.exclude)
    failures = validate(root, files)
    if failures:
        print("\n".join(failures), file=sys.stderr)
        print(f"Found {len(failures)} broken local link(s) across {len(files)} Markdown files.", file=sys.stderr)
        return 1
    print(f"Validated local links in {len(files)} Markdown files; no broken targets or fragments.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
