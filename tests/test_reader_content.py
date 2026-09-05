"""Offline checks for reader-visible punctuation and internal-copy leakage."""

from __future__ import annotations

import pytest

from servicing_brief.reader_content import assert_reader_content, normalize_reader_punctuation


def test_clean_reader_content_preserves_urls_and_accounting_negatives() -> None:
    html = '<p>Loss was ($77m) and ($12m).</p><a href="https://example.test/source—document">Source</a>'
    text = "Loss was ($77m) and ($12m). Source"

    report = assert_reader_content(html, text)

    assert report["valid"] is True
    assert report["semantic_exhaustiveness"] is False
    original = {"headline": "Funding — costs", "href": "https://example.test/source—document", "loss": "−$77m"}
    normalized = normalize_reader_punctuation(original)
    assert normalized["headline"] == "Funding; costs"
    assert normalized["href"] == original["href"]
    assert normalized["loss"] == original["loss"]
    assert original["loss"] == "−$77m"


@pytest.mark.parametrize(
    "html,text",
    [
        ("<p>Result — improved</p>", "Result — improved"),
        ("<p>Result &amp;mdash; improved</p>", "Result improved"),
        ("<p>Result &#x2014; improved</p>", "Result improved"),
    ],
)
def test_literal_and_decoded_html_entity_em_dashes_are_rejected(html: str, text: str) -> None:
    with pytest.raises(ValueError, match="em dash"):
        assert_reader_content(html, text)


@pytest.mark.parametrize("attribute", ["title", "alt", "aria-label", "aria-description", "aria-valuetext", "placeholder"])
def test_reader_attributes_are_checked(attribute: str) -> None:
    html = f'<input {attribute}="Internal — note">'

    with pytest.raises(ValueError, match="em dash"):
        assert_reader_content(html, "Clean reader text")


@pytest.mark.parametrize("tag", ["input", "button"])
def test_input_and_button_values_are_checked(tag: str) -> None:
    html = f'<{tag} value="Internal — label">'

    with pytest.raises(ValueError, match="em dash"):
        assert_reader_content(html, "Clean reader text")


def test_noscript_fallback_is_checked() -> None:
    html = "<noscript>Internal — fallback</noscript><p>Clean</p>"

    with pytest.raises(ValueError, match="visible HTML text"):
        assert_reader_content(html, "Clean")


def test_css_content_escape_and_display_script_escape_are_rejected() -> None:
    css = '<style>.flag::before { content: "\\2014"; }</style><p>Clean</p>'
    with pytest.raises(ValueError, match="CSS style block"):
        assert_reader_content(css, "Clean")

    inline_css = "<p style='content: \"\\2014\"'>Clean</p>"
    with pytest.raises(ValueError, match="inline CSS content"):
        assert_reader_content(inline_css, "Clean")

    script = '<script>const displayLabel = "\\u2014";</script><p>Clean</p>'
    with pytest.raises(ValueError, match="display script"):
        assert_reader_content(script, "Clean")


@pytest.mark.parametrize(
    "html,text",
    [
        ("<p>Why it adds nothing material</p>", "Why it adds nothing material"),
        ("<p>No material incremental insight</p>", "No material incremental insight"),
        ('<span title="Document was reviewed">Clean</span>', "Clean"),
        ("<p>Reviewed archive</p>", "Reviewed archive"),
    ],
)
def test_research_only_phrases_are_rejected_from_reader_surfaces(html: str, text: str) -> None:
    with pytest.raises(ValueError, match="research-only phrase"):
        assert_reader_content(html, text)


def test_normalizer_rewrites_entities_without_touching_numeric_signs() -> None:
    value = "Costs &mdash; rose; loss −$77m; change -$12m"

    assert normalize_reader_punctuation(value) == "Costs; rose; loss −$77m; change -$12m"


@pytest.mark.parametrize('value', ['−$77m', '-$77m', 'C$-1.2bn', '-2.5%', '−12 bps', '-77', '- $77m', '- 2.5%', '- 12 bps'])
def test_negative_financial_display_requires_parentheses(value: str) -> None:
    with pytest.raises(ValueError, match='must use parentheses'):
        assert_reader_content(f'<p>Reported value: {value}</p>', '')
    with pytest.raises(ValueError, match='must use parentheses'):
        assert_reader_content('<p>Clean</p>', f'Reported value: {value}')


def test_accounting_format_preserves_dates_ranges_and_source_addresses() -> None:
    text = '($77m), (2.5%), (12 bps); 2026-09-05; Q1-Q2; pages 11-12; https://example.test/-77'
    assert assert_reader_content(f'<p>{text}</p>', text)['valid']


@pytest.mark.parametrize('subject', ['Result — update', 'Result &mdash; update', 'Loss: -$77m'])
def test_subject_is_part_of_reader_display_gate(subject: str) -> None:
    with pytest.raises(ValueError):
        assert_reader_content('<p>Clean</p>', 'Clean', subject=subject)


def test_css_attribute_generated_text_is_checked() -> None:
    html = '<style>.label::before {content:attr(data-label)}</style><span class="label" data-label="Cost &mdash; change">Clean</span>'
    with pytest.raises(ValueError, match='CSS attr'):
        assert_reader_content(html, '')


@pytest.mark.parametrize('html', [
    '<style>.n::after{content:"-$77m"}</style><p>Clean</p>',
    '<style>.n::after{content:"\\2212 $77m"}</style><p>Clean</p>',
    '<script>document.body.append("-$77m")</script><p>Clean</p>',
    '<p>Change was -.5%.</p>',
])
def test_generated_and_fractional_negatives_require_parentheses(html: str) -> None:
    with pytest.raises(ValueError, match='must use parentheses'):
        assert_reader_content(html, '')


def test_document_list_bullets_are_not_negative_financial_values() -> None:
    assert assert_reader_content('<p>10-Q</p>', 'Documents\n- 10-Q (attached)\n- 8-K (attached)')['valid']
    assert assert_reader_content('<p>Documents</p><p>- 10-Q (attached)</p><p>- 8-K (attached)</p>', '')['valid']
    with pytest.raises(ValueError, match='must use parentheses'):
        assert_reader_content('<p>- $77m valuation effects.</p>', '')
