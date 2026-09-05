from __future__ import annotations

from email.message import EmailMessage
from hashlib import sha256
import json
from pathlib import Path

import pytest

from servicing_brief import delivery
from servicing_brief.reader_value import inventory_html
from servicing_brief.reader_value_release import require_five_company_release, review_file, refresh_report_review, text_review_html


def _review_for(html: str) -> dict:
    items = []
    for block in inventory_html(html):
        utility = block["kind"] in {"metadata", "reference", "heading", "headline"}
        if utility:
            rationales = {
                "reader_need": "Heading identifies the subject for readers.",
                "incremental_value": "It locates the brief for navigation.",
                "inclusion_logic": "Keep this heading for navigation.",
                "reason": "It identifies the reader subject.",
            }
            priority = "utility"
        else:
            rationales = {
                "reader_need": "Readers need this mortgage servicing result for context.",
                "incremental_value": "It adds a specific source-backed servicing performance detail.",
                "inclusion_logic": "Keep this block because it supports the servicing brief.",
                "reason": "It is specific, sourced, and useful for this audience.",
            }
            priority = "primary"
        items.append(
            {
                "id": block["id"],
                "verdict": "keep",
                **rationales,
                "scope": "mortgage_servicing",
                "priority": priority,
                "relevance_bridge": "",
                "inventory_text": block["text"],
            }
        )
    return {
        "version": 1,
        "priority": "P0",
        "html_sha256": sha256(html.encode("utf-8")).hexdigest(),
        "reviewer": "release test reviewer",
        "objective": "mortgage_servicing",
        "items": items,
    }


def _write_review(html_path: Path, review_path: Path) -> None:
    html = html_path.read_text(encoding="utf-8")
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(json.dumps(_review_for(html), indent=2), encoding="utf-8")


def test_plain_text_addition_cannot_inherit_html_approval(tmp_path):
    report = {'html': '<p>Servicing income grew to $22m.</p>', 'text': 'Servicing income grew to $22m.'}
    (tmp_path/'reader-value-review.json').write_text(json.dumps(_review_for(report['html'])))
    (tmp_path/'reader-value-text-review.json').write_text(json.dumps(_review_for(text_review_html(report['text']))))
    assert refresh_report_review(report, tmp_path)['status'] == 'approved'
    report['text'] += '\nUnreviewed banking profitability deserves a highlight.'
    result = refresh_report_review(report, tmp_path)
    assert result['status'] == 'blocked'
    assert any('Plain-text alternative' in reason for reason in result['blockers'])


def test_plain_coverage_note_binds_boundary_to_its_actual_reference():
    from servicing_brief.reader_value import evaluate_reader_value
    text = 'Coverage note\nQ3 banking results do not isolate servicing earnings. [1]\nSources\n[1] Q3 report\nhttps://issuer.example/report.pdf\n'
    html = text_review_html(text, document_kind='coverage_note')
    review = _review_for(html)
    review['document_kind'] = 'coverage_note'
    for item in review['items']:
        item['priority'] = 'supporting'
        if item['inventory_text'].startswith('Q3 banking results'):
            item['scope'] = 'coverage_boundary'
    assert evaluate_reader_value(html, review)['status'] == 'approved'
    changed = text_review_html(text.replace('https://issuer.example/report.pdf','https://issuer.example/other.pdf'), document_kind='coverage_note')
    assert evaluate_reader_value(changed, review)['status'] == 'blocked'


def test_review_file_missing_review_fails(tmp_path: Path) -> None:
    html_path = tmp_path / "brief.html"
    html_path.write_text("<h1>Mortgage servicing brief</h1><p>Servicing result.</p>", encoding="utf-8")

    with pytest.raises(ValueError, match="review record is missing"):
        review_file(html_path, tmp_path / "missing-review.json")


def test_review_file_stale_html_fails(tmp_path: Path) -> None:
    html_path = tmp_path / "brief.html"
    review_path = tmp_path / "review.json"
    html_path.write_text("<h1>Mortgage servicing brief</h1><p>Servicing result.</p>", encoding="utf-8")
    _write_review(html_path, review_path)
    html_path.write_text("<h1>Mortgage servicing brief</h1><p>Servicing result: $101m.</p>", encoding="utf-8")

    with pytest.raises(ValueError, match="html_sha256 does not match"):
        review_file(html_path, review_path)


def test_review_file_revise_verdict_fails(tmp_path: Path) -> None:
    html_path = tmp_path / "brief.html"
    review_path = tmp_path / "review.json"
    html_path.write_text("<h1>Mortgage servicing brief</h1><p>Servicing result.</p>", encoding="utf-8")
    review = _review_for(html_path.read_text(encoding="utf-8"))
    review["items"][1]["verdict"] = "revise"
    review_path.write_text(json.dumps(review, indent=2), encoding="utf-8")

    with pytest.raises(ValueError, match="is marked revise"):
        review_file(html_path, review_path)


def test_review_file_valid_explicit_synthetic_review_succeeds(tmp_path: Path) -> None:
    html_path = tmp_path / "brief.html"
    review_path = tmp_path / "review.json"
    html_path.write_text(
        "<h1>Mortgage servicing brief</h1><p>Servicing result improved this quarter.</p>",
        encoding="utf-8",
    )
    _write_review(html_path, review_path)

    report = review_file(html_path, review_path)

    assert report["status"] == "approved"
    assert report["reviewed_count"] == report["inventory_count"]


def test_five_company_release_requires_compilation_reviews(tmp_path: Path) -> None:
    tickers = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    root = tmp_path / "five-company"
    root.mkdir()
    (root / "selection.json").write_text(json.dumps({"selected_tickers": tickers}), encoding="utf-8")
    for ticker in tickers:
        html_path = root / f"{ticker}-review.html"
        html_path.write_text(
            f"<h1>{ticker} mortgage servicing brief</h1><p>Servicing result improved this quarter.</p>",
            encoding="utf-8",
        )
        _write_review(html_path, root / "reader-value-reviews" / f"{ticker}-review.json")
    (root / "combined-email.html").write_text("<h1>Combined brief</h1><p>Five company results.</p>", encoding="utf-8")
    (root / "gmail-body.html").write_text("<h1>Combined brief</h1><p>Five company results.</p>", encoding="utf-8")

    with pytest.raises(ValueError, match="P0 reader-value release HOLD") as exc_info:
        require_five_company_release(root)

    message = str(exc_info.value)
    assert "combined-email" in message
    assert "gmail-body" in message


@pytest.mark.parametrize('tickers', [[], ['AAA'] * 5, ['AAA', 'BBB', 'CCC', 'DDD'], ['AAA', 'BBB', 'CCC', 'DDD', '../EEE']])
def test_release_cannot_approve_missing_duplicate_or_invalid_companies(tmp_path, tickers):
    (tmp_path/'selection.json').write_text(json.dumps({'selected_tickers': tickers}))
    for stem in ['combined-email', 'gmail-body']:
        path = tmp_path/f'{stem}.html'
        path.write_text('<p>Mortgage servicing earnings increased this quarter.</p>')
        _write_review(path, tmp_path/'reader-value-reviews'/f'{stem}.json')
    with pytest.raises(ValueError, match='exactly five unique valid tickers'):
        require_five_company_release(tmp_path)


def test_blocked_financial_eml_is_rejected_before_smtp_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    message = EmailMessage()
    message["Subject"] = "Mortgage servicing brief"
    message["From"] = "sender@example.com"
    message["To"] = "owner@example.com"
    message["X-Servicing-Reader-Value"] = "blocked"
    message.set_content("Financial mortgage servicing results are awaiting review.")
    path = tmp_path / "financial-blocked.eml"
    path.write_bytes(message.as_bytes())

    opened: list[bool] = []

    def never_smtp(*_args, **_kwargs):
        opened.append(True)
        raise AssertionError("SMTP must not open for a blocked draft")

    monkeypatch.setattr(delivery.smtplib, "SMTP_SSL", never_smtp)
    monkeypatch.setattr(delivery.smtplib, "SMTP", never_smtp)
    monkeypatch.delenv("READER_VALUE_RELEASE_PASSWORD", raising=False)
    config = {
        "_send": True,
        "email": {
            "from_address": "sender@example.com",
            "recipient": "owner@example.com",
            "smtp_host": "smtp.example.com",
            "smtp_port": 465,
            "smtp_security": "ssl",
            "smtp_username": "sender@example.com",
            "smtp_password_env": "READER_VALUE_RELEASE_PASSWORD",
        },
    }

    result = delivery.deliver_messages(config, [path], tmp_path / "state.sqlite")

    assert result[0]["status"] == "company_boundary_hold"
    assert opened == []
