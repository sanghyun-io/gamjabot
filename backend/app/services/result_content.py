"""Bounded presentation of canonical answers. No HTML execution or URL requests."""

import re
import unicodedata
from bisect import bisect_right
from collections import defaultdict
from urllib.parse import urlsplit

from app.services.peer_reply import parse_reply_footer


def code_ranges(source):
    """Match the existing GUI's fenced/inline-code protection for reasoning tags."""
    protected, fence, offset = [], None, 0
    for line in source.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})([^\n]*)", line)
        if fence:
            closing = re.fullmatch(r" {0,3}(`+|~+)[ \t]*(?:\r?\n)?", line)
            if closing and closing[1][0] == fence[0] and len(closing[1]) >= fence[1]:
                protected.append((fence[2], offset + len(line))); fence = None
        elif marker and not (marker[1][0] == "`" and "`" in marker[2]):
            fence = (marker[1][0], len(marker[1]), offset)
        offset += len(line)
    if fence:
        protected.append((fence[2], len(source)))
    starts = [a for a, _ in protected]
    def inside(index):
        i = bisect_right(starts, index)-1
        return i >= 0 and index < protected[i][1]
    runs = [m for m in re.finditer(r"`+", source) if not inside(m.start())]
    positions = defaultdict(list)
    for m in runs:
        positions[len(m[0])].append(m.start())
    cursor = 0
    inline = []
    for opening in runs:
        if opening.start() < cursor:
            continue
        length = len(opening[0]); candidates = positions[length]
        index = bisect_right(candidates, opening.start())
        if index < len(candidates):
            end = candidates[index] + length
            # Inline code cannot span across a fenced block.
            fence_index = bisect_right(starts, opening.start())
            if fence_index == len(starts) or end <= starts[fence_index]:
                inline.append((opening.start(), end)); cursor = end
    return sorted(protected + inline)


def visible_answer(source):
    source, _, _ = parse_reply_footer(source)
    protected = code_ranges(source)
    inside = lambda index: any(a <= index < b for a, b in protected)
    removals, cursor = [], 0
    for marker in re.finditer(r"^ {0,3}<(/?)think>[ \t]*", source, re.I | re.M):
        if marker.start() < cursor or inside(marker.start()):
            continue
        end = marker.end()
        if not marker[1]:
            end = len(source)
            for closing in re.finditer(r"</think>", source[marker.end():], re.I):
                a = marker.end() + closing.start()
                if not inside(a):
                    end = marker.end() + closing.end(); break
        removals.append((marker.start(), end)); cursor = end
    answer, cursor = [], 0
    for a, b in removals:
        answer.append(source[cursor:a]); cursor = b
    answer.append(source[cursor:])
    return "".join(answer)


def safe_url(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 2048
            or any(c.isspace() or unicodedata.category(c).startswith("C") for c in value)):
        return False
    try:
        url = urlsplit(value)
        return bool(url.scheme in {"http", "https"} and url.hostname and url.username is None
                    and url.password is None and (url.port is None or 1 <= url.port <= 65535)
                    and "\\" not in value)
    except ValueError:
        return False


def answer_links(visible):
    # Only visible URL tokens outside code/raw HTML. This is a selection parser,
    # not a Markdown renderer, URL normalizer or network reachability check.
    chars = list(visible)
    for a, b in code_ranges(visible):
        chars[a:b] = " " * (b-a)
    public = "".join(chars)
    public = re.sub(r"<!--.*?(?:-->|$)", " ", public, flags=re.S)
    public = re.sub(r"<(?!https?://)[^>\n]*>", " ", public, flags=re.I)
    found = set()
    for match in re.finditer(r"(?<![\w])https?://[^\s<>\"']+", public):
        value = match[0].rstrip(".,;:!?")
        while value.endswith(")") and value.count(")") > value.count("("):
            value = value[:-1]
        if safe_url(value):
            found.add(value)
    return found
