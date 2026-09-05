from hashlib import sha256

import pytest

from servicing_brief.reader_value import (
    evaluate_reader_value,
    inventory_html,
    require_reader_value,
)


def _review_for(html: str, *, priority: str = "primary") -> dict:
    inventory = inventory_html(html)
    items = []
    for block in inventory:
        item_priority = "utility" if block["kind"] in {"metadata", "reference", "heading", "headline"} and priority == "utility" else priority
        items.append(
            {
                "id": block["id"],
                "verdict": "keep",
                "reader_need": "Readers need this mortgage servicing result for context.",
                "incremental_value": "It adds a specific source-backed detail about servicing performance.",
                "inclusion_logic": "Keep this block because it supports the mortgage servicing brief.",
                "scope": "mortgage_servicing",
                "priority": item_priority,
                "relevance_bridge": "",
                "reason": "It is specific, sourced, and useful for this audience.",
                "inventory_text": block["text"],
            }
        )
    return {
        "version": 1,
        "priority": "P0",
        "html_sha256": sha256(html.encode("utf-8")).hexdigest(),
        "reviewer": "independent reviewer",
        "objective": "mortgage_servicing",
        "items": items,
    }


def _coverage_html(*, source: bool = True, chart: bool = False, financial_table: bool = False) -> str:
    source_markup = '<a href="https://issuer.test/q3-report">[1]</a>' if source else ""
    chart_markup = '<div class="earnings-chart" role="img" aria-label="Mortgage chart"></div>' if chart else ""
    table_markup = (
        '<table class="financial-table"><thead><tr><th>Metric</th><th>Current</th></tr></thead>'
        '<tbody><tr><td>Servicing income</td><td>$22m</td></tr></tbody></table>'
        if financial_table
        else ""
    )
    return (
        "<body><section class=\"company-section\" data-brief-kind=\"coverage_note\">"
        "<h2>Coverage note</h2>"
        "<p>Reviewed Q3 materials do not separately report mortgage servicing earnings. "
        f"{source_markup}</p>"
        f"{chart_markup}{table_markup}"
        "</section></body>"
    )


def _coverage_review_for(html: str) -> dict:
    review = _review_for(html, priority="supporting")
    review["document_kind"] = "coverage_note"
    for item, block in zip(review["items"], inventory_html(html)):
        if block["kind"] in {"metadata", "reference", "heading", "headline"}:
            item.update(
                {
                    "priority": "utility",
                    "scope": "qualification",
                    "reader_need": "The reader needs this label to understand the coverage note.",
                    "incremental_value": "It identifies the note without adding an unsupported financial finding.",
                    "inclusion_logic": "Keep this label for clear coverage-note navigation.",
                    "reason": "It identifies the bounded coverage result for readers.",
                }
            )
        elif block["kind"] == "paragraph":
            item.update(
                {
                    "priority": "supporting",
                    "scope": "coverage_boundary",
                    "reader_need": "Readers need this coverage boundary to interpret the brief honestly.",
                    "incremental_value": "It states what the reviewed materials do not separately report.",
                    "inclusion_logic": "Keep this bounded note because it prevents a false servicing conclusion.",
                    "reason": "It is a specific source-linked coverage boundary for this audience.",
                }
            )
        else:
            item["scope"] = "coverage_support"
    return review


def test_missing_review_blocks_and_require_raises():
    html = "<h1>Mortgage servicing brief</h1><p>Current servicing result.</p>"

    report = evaluate_reader_value(html, None)

    assert report["status"] == "blocked"
    assert any("review record is missing" in item for item in report["blockers"])
    with pytest.raises(ValueError, match="P0 reader-value gate blocked"):
        require_reader_value(html, None)


def test_email_layout_row_does_not_collapse_entire_brief_into_one_item():
    html = '''<table class="paper"><tr><td><h1>Company</h1>
      <p>Servicing earnings rose.</p><p>Borrower arrears increased.</p>
      <table><tr><th>Period</th><th>Amount</th></tr>
      <tr><td>Q2</td><td>$22m</td></tr></table></td></tr></table>'''
    inventory = inventory_html(html)
    assert [item['kind'] for item in inventory] == ['headline', 'paragraph', 'paragraph', 'metric', 'metric']
    assert inventory[-1]['text'] == 'Q2 $22m'


def test_empty_reader_html_cannot_auto_approve():
    html = "<script>const hidden = true;</script>"
    review = _review_for(html)

    report = evaluate_reader_value(html, review)

    assert report["status"] == "blocked"
    assert any("inventory is empty" in item for item in report["blockers"])


def test_new_unreviewed_block_blocks_even_when_review_hash_is_updated():
    old_html = "<h1>Mortgage servicing brief</h1><p>Current servicing result.</p>"
    new_html = old_html + "<p>New delinquency context.</p>"
    review = _review_for(old_html)
    review["html_sha256"] = sha256(new_html.encode("utf-8")).hexdigest()

    report = evaluate_reader_value(new_html, review)

    assert report["status"] == "blocked"
    assert any("has no review record" in item for item in report["blockers"])


def test_changed_numeric_text_blocks_from_item_provenance():
    old_html = "<p>Servicing UPB: $100m.</p>"
    new_html = "<p>Servicing UPB: $101m.</p>"
    review = _review_for(old_html)
    review["html_sha256"] = sha256(new_html.encode("utf-8")).hexdigest()

    report = evaluate_reader_value(new_html, review)

    assert report["status"] == "blocked"
    assert any("text does not match current inventory" in item for item in report["blockers"])


def test_duplicate_review_id_blocks():
    html = "<h1>Mortgage servicing brief</h1><p>Current servicing result.</p>"
    review = _review_for(html)
    review["items"].append(dict(review["items"][0]))

    report = evaluate_reader_value(html, review)

    assert report["status"] == "blocked"
    assert any("duplicate item id" in item for item in report["blockers"])


def test_weak_rationale_blocks():
    html = "<p>Current mortgage servicing result.</p>"
    review = _review_for(html)
    review["items"][0]["reader_need"] = "important"

    report = evaluate_reader_value(html, review)

    assert report["status"] == "blocked"
    assert any("weak or missing reader_need" in item for item in report["blockers"])


def test_source_and_header_metadata_can_use_utility_rationale():
    html = (
        "<h1>Mortgage servicing brief</h1>"
        "<p>Current mortgage servicing result.</p>"
        '<div class="source-row">Report source <a href="https://issuer.test/report">[1]</a></div>'
    )
    review = _review_for(html)
    source_item = review["items"][2]
    source_item.update(
        {
            "priority": "utility",
            "reader_need": "Citation trail for readers.",
            "incremental_value": "Source link supports traceability.",
            "inclusion_logic": "Keep source link for verification.",
            "reason": "It identifies the underlying report.",
        }
    )

    report = evaluate_reader_value(html, review)

    assert report["status"] == "approved"
    assert report["inventory"][2]["kind"] == "reference"


def test_all_utility_review_blocks_without_primary_content():
    html = (
        "<h1>Mortgage servicing brief</h1>"
        '<div class="source-compact"><a href="https://issuer.test/report">Report</a></div>'
    )
    review = _review_for(html, priority="utility")

    report = evaluate_reader_value(html, review)

    assert report["status"] == "blocked"
    assert any("at least one primary reader-content block" in item for item in report["blockers"])


def test_cited_meaningful_paragraph_remains_primary_content():
    html = '<p>Mortgage servicing income improved. <a href="https://issuer.test/report">[1]</a></p>'
    blocks = inventory_html(html)

    assert blocks[0]["kind"] == "paragraph"
    review = _review_for(html)
    assert review["items"][0]["priority"] == "primary"


def test_broader_bank_source_cannot_be_primary():
    html = "<p>Groupwide banking result with mortgage context.</p>"
    review = _review_for(html)
    review["items"][0].update(
        {
            "scope": "broader_bank",
            "priority": "primary",
            "relevance_bridge": "This context explains the mortgage servicing risk signal.",
        }
    )

    report = evaluate_reader_value(html, review)

    assert report["status"] == "blocked"
    assert any("cannot be primary" in item for item in report["blockers"])


def test_complete_specific_review_passes_and_inventory_is_stable():
    html = (
        "<body>"
        "<h1>Mortgage servicing brief</h1>"
        '<div role="img" aria-label="Mortgage delinquency chart"></div>'
        "<table><tr><td>Servicing UPB</td><td>$100m</td></tr></table>"
        "<p>Mortgage servicing result.</p>"
        '<a href="https://issuer.test/report">Report</a>'
        "<div>Reader context note.</div>"
        "</body>"
    )
    blocks = inventory_html(html)
    review = _review_for(html)

    assert [block["id"] for block in blocks] == ["0001", "0002", "0003", "0004", "0005", "0006"]
    assert [block["kind"] for block in blocks] == [
        "headline",
        "chart",
        "metric",
        "paragraph",
        "reference",
        "metadata",
    ]
    assert blocks[4]["links"] == ["https://issuer.test/report"]
    assert evaluate_reader_value(html, review)["status"] == "approved"


def test_coverage_note_approval_is_bounded_and_requires_visible_label_and_source():
    html = _coverage_html()
    review = _coverage_review_for(html)

    result = evaluate_reader_value(html, review)

    assert result["status"] == "approved"
    assert result["document_kind"] == "coverage_note"
    assert result["human_judgment_required"] is True
    assert result["inventory_count"] == result["reviewed_count"]


@pytest.mark.parametrize(
    "kwargs, blocker",
    [
        ({"chart": True}, "cannot contain a chart"),
        ({"financial_table": True}, "cannot contain a financial-table"),
    ],
)
def test_coverage_note_rejects_chart_or_financial_table(kwargs, blocker):
    html = _coverage_html(**kwargs)
    review = _coverage_review_for(html)

    result = evaluate_reader_value(html, review)

    assert result["status"] == "blocked"
    assert any(blocker in item for item in result["blockers"])


def test_coverage_note_rejects_fake_primary_item():
    html = _coverage_html()
    review = _coverage_review_for(html)
    body_item = next(item for item in review["items"] if item["scope"] == "coverage_boundary")
    body_item["priority"] = "primary"

    result = evaluate_reader_value(html, review)

    assert result["status"] == "blocked"
    assert any("coverage_note item" in item and "cannot have primary priority" in item for item in result["blockers"])


def test_coverage_note_rejects_missing_boundary_source():
    html = _coverage_html(source=False)
    review = _coverage_review_for(html)

    result = evaluate_reader_value(html, review)

    assert result["status"] == "blocked"
    assert any("coverage_boundary needs an actual public HTTPS source link" in item for item in result["blockers"])
    assert any("at least one non-utility body item" in item for item in result["blockers"])


def test_mislabeled_broad_profit_coverage_note_still_requires_human_judgment():
    html = _coverage_html()
    html = html.replace(
        "Reviewed Q3 materials do not separately report mortgage servicing earnings.",
        "Groupwide bank profit increased during the quarter.",
    )
    review = _coverage_review_for(html)

    result = evaluate_reader_value(html, review)

    # Structural approval does not establish that the broad-profit sentence is
    # an honest coverage boundary.  A substantive reviewer must make that call.
    assert result["status"] == "approved"
    assert result["human_judgment_required"] is True
