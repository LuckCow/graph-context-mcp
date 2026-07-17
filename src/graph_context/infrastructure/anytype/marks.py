"""Markdown -> chat marks conversion (quirk C11, spike S14).

The chat UI renders message text as PLAIN TEXT (C7), so the model's
markdown links show their literal ``[label](url)`` glyphs. The cure is
the ``marks`` array a message accepts beside ``text``: wire shape
``{"from", "to", "type"}`` plus ``"param"`` for links, offsets into the
text. This module turns the markdown the model writes into that pair --
the inline subset only (links, bare URLs, bold, italic, strikethrough,
inline code); block syntax (headings, lists, fences) passes through as
literal text, and fenced code blocks shield their contents from inline
parsing.

Live behavior pinned by spike S14 (2026-07-17, sidecar, API 2025-11-08),
mirrored by ``mock_server.py``:

    C11a. Offsets are UTF-16 CODE UNITS, not code points: on a text of
          6 code points / 7 UTF-16 units, ``to=7`` lands and ``to=8``
          errors -- the bounds check names the unit.
    C11b. An invalid range (negative, inverted, or past the text's
          UTF-16 length) fails the whole POST with a 500 -- which the
          client RETRIES through its full backoff ladder before raising.
          Every mark leaving this module is therefore validated against
          the emitted text (belt: correct by construction; suspender:
          the final bounds filter).
    C11c. Unknown mark ``type`` strings and param-less links are
          accepted silently (201) -- the server does not vet vocabulary,
          only ranges. A non-list ``marks`` 400s (Go unmarshal error).
    C11d. Marks round-trip verbatim at ``content.marks`` and are part of
          C8's wholesale edit replacement: a PATCH without ``marks``
          drops them.
"""

from __future__ import annotations

import re
from typing import Any

# Leftmost-then-first-listed wins: a line-start fence beats a lone
# backtick at the same position.
_SPECIAL = re.compile(r"^```|`|\[|\*|~~|https?://", re.MULTILINE)

# [label](url) -- label bracket-free, url whitespace-free with one level
# of balanced parens (wiki-style ``.../Foo_(bar)`` targets).
_LINK = re.compile(
    r"\[(?P<label>[^\]\n]+)\]"
    r"\((?P<url>[^()\s]*(?:\([^()\s]*\)[^()\s]*)*)\)"
)
_BARE_URL = re.compile(r"https?://[^\s<>`\]]+")
_TRAILING_PUNCT = ".,;:!?\"'"


def utf16_len(text: str) -> int:
    """UTF-16 code units in ``text`` -- the mark offset unit (C11a)."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def to_marked_text(markdown: str) -> tuple[str, list[dict[str, Any]]]:
    """Plain text plus wire-shape marks for one outbound message.

    Unmatched or malformed syntax stays literal text -- degrading to
    what the chat shows today is always safe, emitting a bad range is
    not (C11b), hence the closing bounds filter.
    """
    text, marks = _parse(markdown)
    limit = utf16_len(text)
    valid = [m for m in marks if 0 <= m["from"] < m["to"] <= limit]
    return text, sorted(valid, key=lambda m: (m["from"], m["to"], m["type"]))


def _trimmed_url(url: str) -> str:
    """A bare URL without the sentence punctuation that clings to it:
    trailing ``.``/``,``/... always, ``)`` only when unbalanced."""
    while True:
        if url and url[-1] in _TRAILING_PUNCT:
            url = url[:-1]
            continue
        if url.endswith(")") and url.count(")") > url.count("("):
            url = url[:-1]
            continue
        return url


def _find_closer(source: str, start: int, delim: str) -> int:
    """The index of ``delim`` closing a span opened at ``start``, or -1.

    CommonMark-lite flanking: the opener must hug its first inner char
    and the closer its last, so list bullets (``* item``) and arithmetic
    (``2 * 3``) never read as emphasis."""
    if start >= len(source) or source[start].isspace():
        return -1
    search = start + 1
    while (found := source.find(delim, search)) != -1:
        if not source[found - 1].isspace():
            return found
        search = found + len(delim)
    return -1


def _parse(source: str) -> tuple[str, list[dict[str, Any]]]:
    out: list[str] = []
    marks: list[dict[str, Any]] = []
    offset = 0
    pos = 0

    def emit(chunk: str) -> None:
        nonlocal offset
        if chunk:
            out.append(chunk)
            offset += utf16_len(chunk)

    def emit_marked(inner: str, mark_type: str, param: str = "") -> None:
        # Recurse so **[label](url)** nests: inner marks shift into
        # place, then the outer mark wraps whatever text they emitted.
        start = offset
        inner_text, inner_marks = _parse(inner)
        marks.extend(
            {**m, "from": m["from"] + start, "to": m["to"] + start}
            for m in inner_marks
        )
        emit(inner_text)
        if offset > start:
            mark: dict[str, Any] = {"from": start, "to": offset, "type": mark_type}
            if param:
                mark["param"] = param
            marks.append(mark)

    while (special := _SPECIAL.search(source, pos)) is not None:
        emit(source[pos:special.start()])
        pos = special.start()
        token = special.group(0)

        if token == "```":
            # Fenced block: verbatim through the closing fence line,
            # fences included -- nothing inside is inline syntax.
            open_end = source.find("\n", pos)
            close = source.find("\n```", open_end) if open_end != -1 else -1
            if close == -1:
                emit(source[pos:])
                pos = len(source)
                continue
            close_end = source.find("\n", close + 1)
            block_end = len(source) if close_end == -1 else close_end
            emit(source[pos:block_end])
            pos = block_end
        elif token == "`":
            end = source.find("`", pos + 1)
            if end > pos + 1 and "\n" not in source[pos + 1:end]:
                start = offset
                emit(source[pos + 1:end])
                marks.append({"from": start, "to": offset, "type": "keyboard"})
                pos = end + 1
            else:
                emit("`")
                pos += 1
        elif token == "[":
            link = _LINK.match(source, pos)
            if link and link.group("url"):
                emit_marked(link.group("label"), "link", link.group("url"))
                pos = link.end()
            else:
                emit("[")
                pos += 1
        elif token == "*":
            if source.startswith("**", pos):
                close = _find_closer(source, pos + 2, "**")
                if close != -1:
                    emit_marked(source[pos + 2:close], "bold")
                    pos = close + 2
                    continue
            close = _find_closer(source, pos + 1, "*")
            if close != -1 and not source.startswith("**", pos):
                emit_marked(source[pos + 1:close], "italic")
                pos = close + 1
            else:
                emit(source[pos:pos + 2] if source.startswith("**", pos) else "*")
                pos += 2 if source.startswith("**", pos) else 1
        elif token == "~~":
            close = _find_closer(source, pos + 2, "~~")
            if close != -1:
                emit_marked(source[pos + 2:close], "strikethrough")
                pos = close + 2
            else:
                emit("~~")
                pos += 2
        else:  # a bare URL -- or a lone scheme prefix, left literal
            matched = _BARE_URL.match(source, pos)
            if matched is None:
                emit(token)
                pos += len(token)
                continue
            url = _trimmed_url(matched.group(0))
            start = offset
            emit(url)
            marks.append(
                {"from": start, "to": offset, "type": "link", "param": url}
            )
            pos += len(url)
    emit(source[pos:])
    return "".join(out), marks
