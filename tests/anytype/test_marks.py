"""Markdown -> marks conversion (quirk C11, spike S14).

Every mark must be valid against the emitted text: the live server 500s
on a bad range (C11b) and the client retries 500s, so an invalid mark
does not fail fast -- it stalls the turn. Ranges here are UTF-16 code
units (C11a), asserted explicitly in the emoji cases.
"""

from __future__ import annotations

from graph_context.infrastructure.anytype.marks import to_marked_text, utf16_len


class TestLinks:
    def test_markdown_link_becomes_label_plus_link_mark(self) -> None:
        # The documented example: "API docs" clickable at [8, 16).
        text, marks = to_marked_text(
            "See the [API docs](https://developers.anytype.io) for details"
        )
        assert text == "See the API docs for details"
        assert marks == [{
            "from": 8, "to": 16, "type": "link",
            "param": "https://developers.anytype.io",
        }]

    def test_bare_url_is_marked_over_itself(self) -> None:
        text, marks = to_marked_text("docs at https://example.com/guide.")
        assert text == "docs at https://example.com/guide."
        assert marks == [{
            "from": 8, "to": 33, "type": "link",
            "param": "https://example.com/guide",  # trailing dot stays text
        }]

    def test_bare_url_keeps_balanced_parens_but_sheds_wrapping_ones(
        self,
    ) -> None:
        text, marks = to_marked_text(
            "(see https://en.wikipedia.org/wiki/Foo_(bar))"
        )
        assert text == "(see https://en.wikipedia.org/wiki/Foo_(bar))"
        (mark,) = marks
        assert mark["param"] == "https://en.wikipedia.org/wiki/Foo_(bar)"
        assert text[mark["from"]:mark["to"]] == mark["param"]

    def test_link_target_with_parens_parses(self) -> None:
        text, marks = to_marked_text(
            "[Foo](https://en.wikipedia.org/wiki/Foo_(bar)) rocks"
        )
        assert text == "Foo rocks"
        assert marks[0]["param"] == "https://en.wikipedia.org/wiki/Foo_(bar)"

    def test_empty_label_or_target_stays_literal(self) -> None:
        # The empty pair stays literal; the target still reads as a
        # bare URL, marked in place.
        assert to_marked_text("[](https://x.example)") == (
            "[](https://x.example)",
            [{"from": 3, "to": 20, "type": "link",
              "param": "https://x.example"}],
        )
        assert to_marked_text("[label]()") == ("[label]()", [])

    def test_lone_scheme_prefix_stays_literal(self) -> None:
        assert to_marked_text("broken https:// link") == (
            "broken https:// link", []
        )


class TestInlineStyles:
    def test_bold_italic_strike_code(self) -> None:
        text, marks = to_marked_text("**b** *i* ~~s~~ `c`")
        assert text == "b i s c"
        assert marks == [
            {"from": 0, "to": 1, "type": "bold"},
            {"from": 2, "to": 3, "type": "italic"},
            {"from": 4, "to": 5, "type": "strikethrough"},
            {"from": 6, "to": 7, "type": "keyboard"},
        ]

    def test_link_nested_in_bold_yields_both_marks(self) -> None:
        text, marks = to_marked_text("**[docs](https://x.example)**")
        assert text == "docs"
        assert marks == [
            {"from": 0, "to": 4, "type": "bold"},
            {"from": 0, "to": 4, "type": "link", "param": "https://x.example"},
        ]

    def test_list_bullets_and_arithmetic_are_not_emphasis(self) -> None:
        source = "* first\n* second\nand 2 * 3 * 4 = 24"
        assert to_marked_text(source) == (source, [])

    def test_unclosed_markers_stay_literal(self) -> None:
        for source in ("**dangling", "*dangling", "~~dangling", "`dangling"):
            assert to_marked_text(source) == (source, [])

    def test_snake_case_underscores_are_never_emphasis(self) -> None:
        source = "call gc_session_key or _private_name"
        assert to_marked_text(source) == (source, [])


class TestCodeShielding:
    def test_inline_code_contents_are_verbatim(self) -> None:
        text, marks = to_marked_text("pass `**kwargs` through")
        assert text == "pass **kwargs through"
        assert marks == [{"from": 5, "to": 13, "type": "keyboard"}]

    def test_fenced_blocks_pass_through_untouched(self) -> None:
        source = (
            "before **bold**\n"
            "```python\n"
            "x = a * b  # [not](a-link) and **not bold**\n"
            "```\n"
            "after [real](https://x.example)"
        )
        text, marks = to_marked_text(source)
        assert "```python\nx = a * b  # [not](a-link) and **not bold**\n```" in text
        assert [m["type"] for m in marks] == ["bold", "link"]

    def test_unterminated_fence_swallows_the_rest_verbatim(self) -> None:
        source = "intro\n```\n**never bold**"
        assert to_marked_text(source) == (source, [])


class TestUtf16Offsets:
    def test_offsets_count_utf16_units_not_code_points(self) -> None:
        # SLIGHTLY SMILING FACE is one code point but two UTF-16 units:
        # the label starts at 3, not 2 (C11a -- live bounds-checked).
        text, marks = to_marked_text("\N{SLIGHTLY SMILING FACE} [x](https://x.example)")
        assert text == "\N{SLIGHTLY SMILING FACE} x"
        assert marks == [
            {"from": 3, "to": 4, "type": "link", "param": "https://x.example"},
        ]

    def test_every_mark_stays_inside_the_utf16_text(self) -> None:
        # Adversarial soup: whatever parses, no range may leave the text
        # (an out-of-bounds range 500s live and stalls the retry ladder).
        source = (
            "\N{PAPERCLIP}**[a](https://x.example/\N{SLIGHTLY SMILING FACE})** "
            "*** `` ~~ [ ] ( ) https:// *x* \N{FAMILY} ~~y~~"
        )
        text, marks = to_marked_text(source)
        limit = utf16_len(text)
        for mark in marks:
            assert 0 <= mark["from"] < mark["to"] <= limit

    def test_plain_text_converts_to_itself(self) -> None:
        assert to_marked_text("no formatting here") == ("no formatting here", [])
        assert to_marked_text("") == ("", [])
