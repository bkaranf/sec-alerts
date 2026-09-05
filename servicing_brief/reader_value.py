"""P0 reader-value inventory and human review gate.

The inventory is deterministic and deliberately boring: it gives a reviewer a
stable ID for every reader-facing block, including text that does not fit a
known semantic block.  The review gate checks coverage and provenance; it does
not infer that a keyword or source link makes a block useful.
"""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import re
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from .reader_content import assert_reader_content


__all__ = ["inventory_html", "evaluate_reader_value", "require_reader_value"]


_REMOVED_TAGS = {"script", "style", "template"}
_BLOCK_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "caption"}
_ACCESSIBLE_ATTRIBUTES = ("aria-label", "aria-description", "aria-valuetext", "alt", "title")
_SOURCE_CONTAINER_CLASSES = {
    "source-row",
    "source-compact",
    "sourcecompact",
    "source_row",
    "source_compact",
}
_REVIEW_VERDICTS = {"keep", "revise", "remove"}
_REVIEW_PRIORITIES = {"primary", "supporting", "utility"}
_UTILITY_KINDS = {"metadata", "reference", "heading", "headline"}
_BRIDGED_SCOPES = {"broader_bank", "eligibility", "annual_mixed"}
_INSIGNIFICANT_WORDS = {
    "a",
    "an",
    "and",
    "because",
    "block",
    "for",
    "from",
    "important",
    "include",
    "included",
    "is",
    "it",
    "keep",
    "needed",
    "reader",
    "relevant",
    "source",
    "the",
    "this",
    "to",
    "useful",
}


def _normalise_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _links(tag: Tag) -> list[str]:
    result: list[str] = []
    for anchor in tag.find_all("a", href=True):
        href = str(anchor.get("href", ""))
        if href and href not in result:
            result.append(href)
    if tag.name == "a" and tag.get("href"):
        href = str(tag.get("href"))
        if href and href not in result:
            result.insert(0, href)
    return result


def _tag_text(tag: Tag) -> str:
    text = _normalise_text(tag.get_text(" ", strip=True))
    if text:
        return text
    for attribute in _ACCESSIBLE_ATTRIBUTES:
        value = tag.get(attribute)
        if value:
            return _normalise_text(str(value))
    if tag.name in {"input", "button"} and tag.get("value"):
        return _normalise_text(str(tag.get("value")))
    return ""


def _class_tokens(tag: Tag) -> set[str]:
    value = tag.get("class") or []
    if isinstance(value, str):
        value = value.split()
    return {str(item).casefold() for item in value}


def _is_source_container(tag: Tag) -> bool:
    return bool(_class_tokens(tag) & _SOURCE_CONTAINER_CLASSES)


def _only_anchor_content(tag: Tag) -> bool:
    """Return true when a block's nonblank content is contained in anchors."""

    anchors = tag.find_all("a")
    if not anchors:
        return False
    for string in tag.find_all(string=True):
        if isinstance(string, Comment):
            continue
        if not _normalise_text(str(string)):
            continue
        owner = string.parent
        if owner is None or (owner.name != "a" and owner.find_parent("a") is None):
            return False
    return True


def _kind(tag: Tag, text: str, links: list[str]) -> str:
    name = tag.name.lower()
    if _is_source_container(tag):
        return "reference"
    if name == "h1" or name == "h2":
        return "headline"
    if name in {"h3", "h4", "h5", "h6"}:
        return "heading"
    if name == "tr":
        return "metric"
    if name == "caption":
        return "metadata"
    if name == "a" or (name in {"p", "li"} and _only_anchor_content(tag)):
        return "reference"
    if name in {"img", "input", "button"}:
        return "metadata"
    return "paragraph"


def inventory_html(html: str) -> list[dict[str, Any]]:
    """Return stable reader-facing blocks from an HTML document.

    Script, style, and template contents are removed.  ``noscript`` remains in
    the walk because it is the visible fallback for readers without scripting.
    A chart container is one block, a table row is one block, and every other
    nonblank text node outside a known block is retained as ``metadata``.
    """

    if not isinstance(html, str):
        raise ValueError("reader HTML must be text")
    soup = BeautifulSoup(html, "html.parser")
    for element in soup.find_all(list(_REMOVED_TAGS)):
        element.decompose()
    root = soup.body if soup.body is not None else soup
    blocks: list[dict[str, Any]] = []

    def add(tag: Tag, kind: str | None = None, *, text: str | None = None) -> None:
        value = _normalise_text(text if text is not None else _tag_text(tag))
        if not value:
            return
        blocks.append(
            {
                "id": f"{len(blocks) + 1:04d}",
                "kind": kind or _kind(tag, value, _links(tag)),
                "text": value,
                "links": _links(tag),
            }
        )

    def walk(parent: Tag) -> None:
        for child in parent.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                value = _normalise_text(str(child))
                if value:
                    blocks.append(
                        {
                            "id": f"{len(blocks) + 1:04d}",
                            "kind": "metadata",
                            "text": value,
                            "links": [],
                        }
                    )
                continue
            if not isinstance(child, Tag):
                continue
            if child.name.lower() in _REMOVED_TAGS:
                continue
            if str(child.get("role", "")).casefold() == "img" or "earnings-chart" in _class_tokens(child):
                add(child, "chart")
                continue
            if _is_source_container(child):
                add(child, "reference")
                continue
            if child.name.lower() == "tr":
                table = child.find_parent('table')
                is_layout = (
                    table is not None and (
                        str(table.get('role', '')).casefold() == 'presentation'
                        or bool(_class_tokens(table) & {'paper', 'email-shell', 'layout'})
                    )
                ) or child.find(['table', 'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6']) is not None
                if is_layout:
                    walk(child)
                    continue
                add(child, "metric")
                continue
            if child.name.lower() in _BLOCK_TAGS:
                add(child)
                continue
            if child.name.lower() in {"a", "img", "input", "button"}:
                add(child)
                continue
            walk(child)

    walk(root)
    return blocks


def _hidden(tag: Tag) -> bool:
    """Return whether a tag is hidden by an HTML or inline-style ancestor."""

    current: Tag | None = tag
    while isinstance(current, Tag):
        if current.has_attr("hidden"):
            return True
        if str(current.get("aria-hidden", "")).casefold() == "true":
            return True
        style = re.sub(r"\s+", "", str(current.get("style", "")).casefold())
        if "display:none" in style or "visibility:hidden" in style:
            return True
        current = current.parent if isinstance(current.parent, Tag) else None
    return False


def _visible_tag_text(tag: Tag) -> str:
    """Extract visible text while excluding hidden descendants."""

    pieces: list[str] = []
    for string in tag.find_all(string=True):
        owner = string.parent
        if isinstance(owner, Tag) and not _hidden(owner):
            pieces.append(str(string))
    return _normalise_text(" ".join(pieces))


def _public_https_links(value: Any) -> list[str]:
    """Return source-like public HTTPS links from an inventory block."""

    if not isinstance(value, list):
        return []
    links: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            continue
        link = raw.strip()
        parsed = urlparse(link)
        host = (parsed.hostname or "").casefold()
        if (
            parsed.scheme.casefold() != "https"
            or not parsed.netloc
            or any(char.isspace() for char in link)
            or host in {"localhost", "127.0.0.1", "::1"}
        ):
            continue
        if link not in links:
            links.append(link)
    return links


def _coverage_structure(html: str, inventory: list[dict[str, Any]]) -> list[str]:
    """Return fail-closed structural errors for a coverage-only document.

    Coverage notes deliberately have a smaller contract than analytical
    briefs.  Keep this check tied to the rendered HTML so a review record cannot
    turn an arbitrary document into a coverage note by changing metadata alone.
    """

    soup = BeautifulSoup(html, "html.parser")
    sections = soup.select('.company-section[data-brief-kind="coverage_note"]')
    errors: list[str] = []
    if len(sections) != 1:
        errors.append(
            "coverage_note review requires exactly one .company-section[data-brief-kind='coverage_note']"
        )
    all_company_sections = soup.select(".company-section")
    if len(all_company_sections) != 1:
        errors.append("coverage_note review requires exactly one company section")
    if sections:
        section = sections[0]
        label_tags = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "span", "strong", "label", "li"}
        has_label = any(
            element.name in label_tags
            and not _hidden(element)
            and _visible_tag_text(element) == "Coverage note"
            for element in section.find_all(label_tags)
        )
        if not has_label:
            errors.append("coverage_note review requires a visible 'Coverage note' label")

    chart_found = any(item.get("kind") == "chart" for item in inventory)
    financial_table_found = False
    for element in soup.find_all(True):
        classes = _class_tokens(element)
        class_text = " ".join(classes)
        if "chart" in classes or "earnings-chart" in classes or "chart" in class_text:
            chart_found = True
        if (
            "financial-table" in classes
            or "financial-table" in class_text
            or element.has_attr("data-metric-group")
            or element.has_attr("data-metric-index")
        ):
            financial_table_found = True
        if element.name in {"svg", "canvas"}:
            chart_found = True
        if str(element.get("role", "")).casefold() == "img":
            chart_found = True
        if element.name == "table":
            role = str(element.get("role", "")).casefold()
            if role != "presentation" and (element.find("th") is not None or element.find(attrs={"data-metric-index": True}) is not None):
                financial_table_found = True
    if chart_found:
        errors.append("coverage_note review cannot contain a chart")
    if financial_table_found:
        errors.append("coverage_note review cannot contain a financial-table")
    return errors


def _words(value: str) -> list[str]:
    return re.findall(r"\b[\w’$%+]+(?:[-][\w]+)*\b", value, flags=re.UNICODE)


def _substantive(value: Any, *, utility: bool = False) -> bool:
    if not isinstance(value, str):
        return False
    words = _words(value)
    minimum = 3 if utility else 5
    if len(words) < minimum:
        return False
    meaningful = [word.casefold() for word in words if word.casefold() not in _INSIGNIFICANT_WORDS]
    if not meaningful:
        return False
    # A single generic keyword is not a reader-value rationale.  This remains
    # a structural check; the reviewer still decides whether the rationale is
    # factually and editorially sound.
    return len(set(meaningful)) >= (1 if utility else 2)


def _blocker(blockers: list[str], message: str) -> None:
    blockers.append(f"P0: {message}")


def evaluate_reader_value(html: str, review: Mapping[str, Any] | None) -> dict[str, Any]:
    """Evaluate a human reader-value review against current HTML bytes."""

    if not isinstance(html, str):
        raise ValueError("reader HTML must be text")
    inventory = inventory_html(html)
    actual_hash = sha256(html.encode("utf-8")).hexdigest()
    blockers: list[str] = []
    try:
        assert_reader_content(html, '')
    except ValueError as exc:
        blockers.append(f'P0 reader display: {exc}')

    if review is None:
        _blocker(blockers, "reader-value review record is missing")
        return {
            "status": "blocked",
            "html_sha256": actual_hash,
            "inventory_count": len(inventory),
            "reviewed_count": 0,
            "document_kind": "analysis",
            "human_judgment_required": True,
            "blockers": blockers,
            "inventory": inventory,
        }
    if not isinstance(review, Mapping):
        raise ValueError("reader-value review must be an object or None")
    if review.get("version") != 1:
        _blocker(blockers, "review version must be 1")
    if review.get("priority") != "P0":
        _blocker(blockers, "review priority must be P0")
    if review.get("objective") != "mortgage_servicing":
        _blocker(blockers, "review objective must be mortgage_servicing")
    if not isinstance(review.get("reviewer"), str) or not review.get("reviewer", "").strip():
        _blocker(blockers, "reviewer is required")

    document_kind = review.get("document_kind", "analysis")
    if document_kind in (None, ""):
        document_kind = "analysis"
    if not isinstance(document_kind, str) or document_kind not in {"analysis", "coverage_note"}:
        _blocker(blockers, "review document_kind must be analysis or coverage_note")
        document_kind = "analysis"
    coverage_note = document_kind == "coverage_note"
    if coverage_note:
        for error in _coverage_structure(html, inventory):
            _blocker(blockers, error)

    expected_hash = review.get("html_sha256")
    if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash.strip()):
        _blocker(blockers, "review html_sha256 is missing or malformed")
    elif expected_hash.strip().lower() != actual_hash:
        _blocker(blockers, "review html_sha256 does not match the current HTML")

    items = review.get("items")
    if not isinstance(items, list):
        _blocker(blockers, "review items must be a list")
        items = []

    inventory_by_id = {str(item["id"]): item for item in inventory}
    review_ids: list[str] = []
    for position, item in enumerate(items):
        if not isinstance(item, Mapping):
            _blocker(blockers, f"review item {position + 1} is not an object")
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id.strip():
            _blocker(blockers, f"review item {position + 1} has no id")
            continue
        item_id = item_id.strip()
        review_ids.append(item_id)
        if review_ids.count(item_id) > 1:
            _blocker(blockers, f"review contains duplicate item id {item_id}")
        if coverage_note and item.get("priority") == "primary":
            _blocker(blockers, f"coverage_note item {item_id} cannot have primary priority")

    inventory_ids = set(inventory_by_id)
    if not inventory:
        _blocker(blockers, "reader HTML inventory is empty")
    review_id_set = set(review_ids)
    for missing in sorted(inventory_ids - review_id_set):
        _blocker(blockers, f"inventory item {missing} has no review record")
    for extra in sorted(review_id_set - inventory_ids):
        _blocker(blockers, f"review item {extra} is not present in current inventory")

    has_primary_content = False
    has_coverage_boundary = False
    for position, item in enumerate(items):
        if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
            continue
        item_id = item["id"].strip()
        current = inventory_by_id.get(item_id)
        if current is None:
            continue
        kind = str(current.get("kind", "metadata"))
        priority = item.get("priority")
        if priority not in _REVIEW_PRIORITIES:
            _blocker(blockers, f"item {item_id} priority must be primary, supporting, or utility")
            priority = "supporting"
        utility = priority == "utility" and kind in _UTILITY_KINDS

        verdict = item.get("verdict")
        if verdict not in _REVIEW_VERDICTS:
            _blocker(blockers, f"item {item_id} verdict must be keep, revise, or remove")
        elif verdict != "keep":
            _blocker(blockers, f"item {item_id} is marked {verdict}")

        for field in ("reader_need", "incremental_value", "inclusion_logic", "reason"):
            value = item.get(field)
            if not _substantive(value, utility=utility):
                _blocker(blockers, f"item {item_id} has a weak or missing {field} rationale")

        scope = item.get("scope")
        if not isinstance(scope, str) or not scope.strip():
            _blocker(blockers, f"item {item_id} scope is required")
            scope = ""
        if scope in {"broader_bank", "eligibility", "annual_mixed"}:
            bridge = item.get("relevance_bridge")
            if not isinstance(bridge, str) or not bridge.strip():
                _blocker(blockers, f"item {item_id} requires a relevance_bridge for scope {scope}")
            elif not _substantive(bridge):
                _blocker(blockers, f"item {item_id} has a weak relevance_bridge for scope {scope}")
            if priority == "primary":
                _blocker(blockers, f"item {item_id} broad or mixed scope cannot be primary")
        elif item.get("relevance_bridge") not in (None, "") and not isinstance(item.get("relevance_bridge"), str):
            _blocker(blockers, f"item {item_id} relevance_bridge must be text")

        item_hash = item.get("html_sha256")
        if item_hash not in (None, "") and str(item_hash).strip().lower() != actual_hash:
            _blocker(blockers, f"item {item_id} html_sha256 does not match the current HTML")
        expected_text = item.get("inventory_text", item.get("text"))
        if expected_text not in (None, "") and expected_text != current.get("text"):
            _blocker(blockers, f"item {item_id} text does not match current inventory")

        if (
            coverage_note
            and verdict == "keep"
            and priority == "supporting"
            and kind not in _UTILITY_KINDS
            and scope == "coverage_boundary"
        ):
            if _public_https_links(current.get("links")):
                has_coverage_boundary = True
            else:
                _blocker(
                    blockers,
                    f"item {item_id} coverage_boundary needs an actual public HTTPS source link",
                )

        if (
            priority == "primary"
            and verdict == "keep"
            and kind not in _UTILITY_KINDS
            and scope not in _BRIDGED_SCOPES
        ):
            has_primary_content = True

    if inventory and not coverage_note and not has_primary_content:
        _blocker(blockers, "review must keep at least one primary reader-content block")
    if coverage_note and not has_coverage_boundary:
        _blocker(
            blockers,
            "coverage_note must keep at least one non-utility body item with scope coverage_boundary and a source link",
        )

    return {
        "status": "approved" if not blockers else "blocked",
        "html_sha256": actual_hash,
        "inventory_count": len(inventory),
        "reviewed_count": len(items),
        "document_kind": document_kind,
        "human_judgment_required": True,
        "blockers": blockers,
        "inventory": inventory,
    }


def require_reader_value(html: str, review: Mapping[str, Any] | None) -> dict[str, Any]:
    """Require an approved P0 review or raise a human-readable ValueError."""

    report = evaluate_reader_value(html, review)
    if report["status"] != "approved":
        blockers = "; ".join(report["blockers"]) or "review did not pass"
        raise ValueError(f"P0 reader-value gate blocked: {blockers}")
    return report
