"""Checks for accidental internal copy in reader-facing HTML and text.

This module is intentionally independent of the renderer.  It checks the
surfaces a reader can see, plus conservative CSS and display-script surfaces,
without rewriting source addresses or signed evidence. Negative financial
display values must use accounting parentheses.
"""

from __future__ import annotations

from collections.abc import Mapping
from html import unescape
import re
from typing import Any

from bs4 import BeautifulSoup


__all__ = ["assert_reader_content", "assert_reader_mime", "normalize_reader_punctuation"]


_READER_ATTRIBUTES = ("title", "alt", "aria-label", "aria-description", "aria-valuetext", "placeholder")
_URL_KEYS = {"href", "url", "source_url", "link", "src"}
_NEGATIVE_DISPLAY = re.compile(r"(?<![\w./-])(?:[−-]\s*(?:[A-Z]{0,3}\s*[$€£]\s*)?|[A-Z]{0,3}\s*[$€£]\s*[−-]\s*)(?:\d[\d,]*(?:\.\d+)?|\.\d+)")
_CONTENT_VALUE = re.compile(r'''\bcontent\s*:\s*((?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^;}])*)''', re.I)
_SCRIPT_STRING = re.compile(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`''')


def _decode_display_escapes(value: str) -> str:
    value = re.sub(r'\\u(?:\{([0-9a-f]{1,6})\}|([0-9a-f]{4}))', lambda m: chr(int(m[1] or m[2], 16)), value, flags=re.I)
    return re.sub(r'\\([0-9a-f]{1,6})(?:\s)?', lambda m: chr(int(m[1], 16)) if int(m[1], 16) <= 0x10ffff else m[0], value, flags=re.I)


def _assert_accounting_display(value: str, surface: str) -> None:
    # Addresses are evidence identifiers, not financial display strings.
    prose = re.sub(r"https?://\S+", "", unescape(value))
    # Only explicit filing-form labels get this plain-text bullet exception.
    # A leading '- $77m' or '- 2.5%' must still fail as negative display.
    prose = re.sub(r'(?<![\w/])-\s+(?=(?:10-[KQ]|8-K|20-F|6-K)\b)', '', prose)
    match = _NEGATIVE_DISPLAY.search(prose)
    if match:
        raise ValueError(f"negative financial display must use parentheses in {surface}: {match[0]}")

# HTML entities are decoded before the visible-text check, but retaining the
# forms here also catches script/style text that parsers intentionally leave
# untouched.
_EM_DASH_ENTITY = re.compile(r"&(?:mdash|#8212|#x2014);", re.IGNORECASE)
_EM_DASH_ESCAPE = re.compile(
    r"\\(?:u(?:\{0*2014\}|0*2014)|x(?:\{0*2014\}|2014)|0*2014)(?:\s)?",
    re.IGNORECASE,
)
_RESEARCH_ONLY_PHRASES = (
    "why it adds nothing material",
    "no material incremental insight",
    "document was reviewed",
    "reviewed archive",
)


def _contains_em_dash(value: str, *, escaped: bool = False) -> bool:
    """Check literal/entity dashes, and optional escaped Unicode forms."""

    decoded = unescape(value)
    if "—" in decoded or _EM_DASH_ENTITY.search(value):
        return True
    return escaped and bool(_EM_DASH_ESCAPE.search(value))


def _research_phrase(value: str) -> str | None:
    lowered = unescape(value).casefold()
    for phrase in _RESEARCH_ONLY_PHRASES:
        if phrase in lowered:
            return phrase
    return None


def _surface_excerpt(value: str, needle: str | None = None) -> str:
    compact = re.sub(r"\s+", " ", value).strip()
    if needle and needle in compact:
        start = max(0, compact.find(needle) - 40)
        return compact[start : start + 160]
    return compact[:160]


def _dash_failure(surface: str, value: str) -> ValueError:
    return ValueError(f"reader content contains an em dash in {surface}: {_surface_excerpt(value)}")


def _research_failure(surface: str, value: str, phrase: str) -> ValueError:
    return ValueError(f"reader content contains research-only phrase {phrase!r} in {surface}: {_surface_excerpt(value, phrase)}")


def normalize_reader_punctuation(value: Any, *, _key: str = "") -> Any:
    """Replace em-dash prose punctuation while preserving URLs and numbers.

    This helper only changes literal em dashes and their common HTML entities.
    URL-like values are returned byte-for-byte, and Unicode minus (``−``),
    hyphen-minus negatives (``-$77m``), and other numeric characters are never
    rewritten.
    """

    if isinstance(value, str):
        if _key.casefold() in _URL_KEYS or value.startswith(("https://", "http://")):
            return value
        return re.sub(r"\s*(?:—|&mdash;|&#8212;|&#x2014;)\s*", "; ", value, flags=re.IGNORECASE)
    if isinstance(value, list):
        return [normalize_reader_punctuation(item, _key=_key) for item in value]
    if isinstance(value, tuple):
        return tuple(normalize_reader_punctuation(item, _key=_key) for item in value)
    if isinstance(value, Mapping):
        return {
            key: item if str(key).casefold() in _URL_KEYS else normalize_reader_punctuation(item, _key=str(key))
            for key, item in value.items()
        }
    return value


def assert_reader_content(html: str, text: str, *, subject: str = "") -> dict[str, Any]:
    """Validate reader-visible copy and return a compact audit summary.

    The check raises ``ValueError`` on a literal or entity-decoded em dash,
    common escaped Unicode dash forms in style/display-script surfaces, or an
    obvious research-only phrase.  It is deliberately conservative and does
    not claim semantic exhaustiveness.
    """

    if not isinstance(html, str):
        raise ValueError("reader html must be text")
    if not isinstance(text, str):
        raise ValueError("reader text must be text")

    if _contains_em_dash(subject):
        raise _dash_failure("email subject", subject)
    _assert_accounting_display(subject, "email subject")
    _assert_accounting_display(text, "plain text")

    if _contains_em_dash(text):
        raise _dash_failure("plain text", text)
    phrase = _research_phrase(text)
    if phrase:
        raise _research_failure("plain text", text, phrase)

    soup = BeautifulSoup(html, "html.parser")
    # Keep noscript: it is the visible fallback when scripting is disabled.
    for element in soup(["script", "style", "template"]):
        element.extract()
    visible = soup.get_text(" ", strip=True)
    _assert_accounting_display(visible, "visible HTML text")
    if _contains_em_dash(visible):
        raise _dash_failure("visible HTML text", visible)
    phrase = _research_phrase(visible)
    if phrase:
        raise _research_failure("visible HTML text", visible, phrase)

    checked_attributes = 0
    for element in BeautifulSoup(html, "html.parser").find_all(True):
        attributes = _READER_ATTRIBUTES
        if element.name in {"input", "button"}:
            attributes = (*_READER_ATTRIBUTES, "value")
        for attribute in attributes:
            if not element.has_attr(attribute):
                continue
            checked_attributes += 1
            value = element.get(attribute)
            if isinstance(value, (list, tuple)):
                value = " ".join(str(item) for item in value)
            else:
                value = str(value)
            surface = f"{attribute} attribute"
            _assert_accounting_display(value, surface)
            if _contains_em_dash(value):
                raise _dash_failure(surface, value)
            phrase = _research_phrase(value)
            if phrase:
                raise _research_failure(surface, value, phrase)

    checked_styles = 0
    checked_scripts = 0
    raw_soup = BeautifulSoup(html, "html.parser")
    # CSS attr() can expose attributes that are otherwise only internal data.
    css_sources = [str(e) for e in raw_soup.find_all('style')]
    css_sources += [str(e['style']) for e in raw_soup.find_all(style=True)]
    exposed_attributes = set(re.findall(r'\battr\(\s*([\w-]+)', ' '.join(css_sources), re.I))
    for attribute in exposed_attributes:
        for element in raw_soup.find_all(attrs={attribute: True}):
            value = str(element[attribute])
            if _contains_em_dash(value):
                raise _dash_failure(f'CSS attr({attribute})', value)
            _assert_accounting_display(value, f'CSS attr({attribute})')
    for element in raw_soup.find_all("style"):
        checked_styles += 1
        value = element.get_text(" ", strip=False)
        for content in _CONTENT_VALUE.findall(value):
            _assert_accounting_display(_decode_display_escapes(content), 'CSS generated content')
        if _contains_em_dash(value, escaped=True):
            raise _dash_failure("CSS style block", value)
        phrase = _research_phrase(value)
        if phrase:
            raise _research_failure("CSS style block", value, phrase)

    # Inline pseudo-content is less common in the generated email, but checking
    # it when a content property is present closes the same reader-visible gap.
    for element in raw_soup.find_all(style=True):
        value = str(element.get("style", ""))
        if not re.search(r"\bcontent\s*:", value, re.IGNORECASE):
            continue
        for content in _CONTENT_VALUE.findall(value):
            _assert_accounting_display(_decode_display_escapes(content), 'inline CSS generated content')
        checked_styles += 1
        if _contains_em_dash(value, escaped=True):
            raise _dash_failure("inline CSS content", value)
        phrase = _research_phrase(value)
        if phrase:
            raise _research_failure("inline CSS content", value, phrase)

    for element in raw_soup.find_all("script"):
        checked_scripts += 1
        value = element.get_text(" ", strip=False)
        for literal in _SCRIPT_STRING.findall(value):
            _assert_accounting_display(_decode_display_escapes(literal), 'display script string')
        if _contains_em_dash(value, escaped=True):
            raise _dash_failure("display script", value)
        phrase = _research_phrase(value)
        if phrase:
            raise _research_failure("display script", value, phrase)

    return {
        "valid": True,
        "html_chars": len(html),
        "text_chars": len(text),
        "visible_text_chars": len(visible),
        "reader_attributes_checked": checked_attributes,
        "style_blocks_checked": checked_styles,
        "display_scripts_checked": checked_scripts,
        "scope": "reader-visible text and presentation surfaces; URL values are excluded",
        "semantic_exhaustiveness": False,
        "negative_display": "parentheses",
    }


def assert_reader_mime(message: Any) -> dict[str, Any]:
    """Check the actual decoded email alternatives after all packaging edits."""
    bodies = {}
    for kind in ("html", "plain"):
        part = message.get_body(preferencelist=(kind,))
        bodies[kind] = part.get_content() if part is not None else ""
    return assert_reader_content(bodies["html"], bodies["plain"], subject=str(message.get("Subject", "")))
