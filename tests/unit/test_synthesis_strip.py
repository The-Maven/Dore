"""Regression: synthesize_surface must strip raw HTML tags the LLM
occasionally emits when it falls out of pure-markdown mode.

User report: the redemptions page showed visible `<br>` text in the
narrative. The client-side markdown() pipeline html-escapes its input
so a literal `<br>` arrives as `&lt;br&gt;` and renders as text. This
test pins the server-side cleaning so future writes are tag-free.
"""
from __future__ import annotations

from sca.agent.synthesis import _strip_html_tags


def test_br_tags_become_newlines():
    """The user's bug: `<br>` showing up literally in redemption text.
    The cleaner must convert them to newlines so markdown renders a
    proper paragraph or list break."""
    src = "First line.<br>Second line.<br/>Third<br />Fourth"
    out = _strip_html_tags(src)
    assert "<br" not in out.lower()
    assert "First line." in out
    assert "Second line." in out
    assert "Third" in out
    assert "Fourth" in out
    # Newlines now separate the lines so markdown renders them properly
    assert "\n" in out


def test_p_and_div_tags_become_newlines():
    """<p>, </p>, <div>, </div> — same pipeline trips on these too."""
    src = "<p>Para one.</p><p>Para two.</p><div>Div block.</div>"
    out = _strip_html_tags(src)
    assert "<p" not in out.lower()
    assert "</p" not in out.lower()
    assert "<div" not in out.lower()
    assert "Para one." in out
    assert "Para two." in out
    assert "Div block." in out


def test_inline_emphasis_tags_are_stripped_keeping_content():
    """<strong>, <em>, <b>, <i> — the inner text matters, the tags do
    not. Drop the tags, keep the words."""
    src = "<strong>Bold word</strong> and <em>italic</em> and <b>bold2</b>"
    out = _strip_html_tags(src)
    assert "<strong" not in out.lower()
    assert "<em" not in out.lower()
    assert "<b>" not in out.lower()
    assert "Bold word" in out
    assert "italic" in out
    assert "bold2" in out


def test_runs_of_newlines_collapse_to_paragraph_break():
    """LLMs occasionally emit triple newlines or `<br><br><br>` runs.
    Collapse to at most a single paragraph break so the rendered
    markdown does not produce visually-jarring gaps."""
    src = "A.<br><br><br>B.<br><br>C."
    out = _strip_html_tags(src)
    # No more than two consecutive newlines anywhere
    assert "\n\n\n" not in out


def test_clean_markdown_passes_through_untouched():
    """A pure-markdown narrative (the common case) must NOT be altered
    by the cleaner. Pin: dollar signs, asterisks, brackets stay."""
    src = (
        "**USDC** trades at $1.00 with cone width *2bp*. See "
        "[Circle](https://circle.com) and `[corpus-id §1.2]` for details."
    )
    out = _strip_html_tags(src)
    assert out == src


def test_none_and_empty_return_safe():
    """The cleaner is called on every synthesis return — must handle
    edge cases without crashing."""
    assert _strip_html_tags("") == ""
    assert _strip_html_tags(None) is None


def test_mixed_real_world_llm_output():
    """A realistic example pulled from the user-reported failure
    mode: prose with stray <br> tags in the middle."""
    src = (
        "USDC's liquid coverage stands at 102.3% as of the latest "
        "attestation.<br><br>The reserve is 78% Treasury bills, 22% "
        "cash deposits at regulated banks.<br>Net redemption flow "
        "over the last 7 days was -$420M, within historical norms."
    )
    out = _strip_html_tags(src)
    assert "<br" not in out.lower()
    # All three statements survive
    assert "102.3%" in out
    assert "Treasury bills" in out
    assert "-$420M" in out
