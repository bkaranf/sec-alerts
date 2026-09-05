from __future__ import annotations

from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from hashlib import sha256
from pathlib import Path

import pytest

from servicing_brief.company_boundary import (
    BOUNDARY_HEADER,
    BOUNDARY_VERSION,
    CompanyBoundaryError,
    BriefIdentity,
    IDENTITY_HEADER,
    TEST_HEADER,
    require_company_boundary,
    validate_mime_message,
    validate_report_surfaces,
    validate_source_documents,
)
from servicing_brief import delivery
from servicing_brief.models import Document


def _report(*, html: str | None = None, text: str | None = None, reports: dict | None = None) -> dict:
    html_body = html or "<html><body data-brief-company='TD' data-brief-cik='0000947263' data-brief-event='2026-Q3'><p>TD result.</p></body></html>"
    text_body = text or "Company: The Toronto-Dominion Bank (TD)\nEarnings period: Q3 2026\n\nTD result."
    return {
        "company_identity": {
            "ticker": "TD",
            "cik": "0000947263",
            "event": "2026-Q3",
            "kind": "earnings_brief",
        },
        "company_reports": reports if reports is not None else {"TD": {"html": html_body, "text": text_body}},
        "html": html_body,
        "text": text_body,
        "subject": "TD earnings",
    }


def _document(tmp_path: Path, *, name: str, cik: str = "0000947263", ticker: str = "TD", kind: str = "release", period: str = "2026-Q3", context: bool = False) -> Document:
    raw = f"{name}-{cik}-{ticker}-{kind}-{period}".encode()
    path = tmp_path / f"{name}.html"
    path.write_bytes(raw)
    return Document(
        issuer="The Toronto-Dominion Bank",
        cik=cik,
        title=name,
        kind=kind,
        source="ir",
        url=f"https://example.test/{name}",
        published="2026-08-27",
        period=period,
        path=str(path),
        content_hash=sha256(raw).hexdigest(),
        metadata={"ticker": ticker, **({"context": True} if context else {})},
    )


def test_strict_boundary_accepts_one_company_event_and_explicit_annual_context(tmp_path: Path) -> None:
    current = _document(tmp_path, name="q3-release")
    annual = _document(tmp_path, name="fy25", kind="10-K", period="2025-FY", context=True)

    result = require_company_boundary(_report(), [current], context_documents=[annual])

    assert result["identity"]["ticker"] == "TD"
    assert result["identity"]["cik"] == "0000947263"
    assert result["sources"]["events"] == ["2026-Q3"]


@pytest.mark.parametrize("include_primary_context", [False, True])
def test_context_alone_cannot_establish_current_earnings_event(tmp_path: Path, include_primary_context: bool) -> None:
    annual = _document(tmp_path, name="fy25", kind="10-K", period="2025-FY", context=True)
    primary = [annual] if include_primary_context else []
    with pytest.raises(CompanyBoundaryError, match="no current earnings-event source"):
        require_company_boundary(_report(), primary, context_documents=[annual])


def test_context_before_release_does_not_choose_the_context_period(tmp_path: Path) -> None:
    annual = _document(tmp_path, name="fy25", kind="10-K", period="2025-FY", context=True)
    current = _document(tmp_path, name="q3-release")
    result = validate_source_documents([annual, current])
    assert result["identity"]["event"] == "2026-Q3"


@pytest.mark.parametrize(
    "change, expected",
    [
        (lambda report: report.update(company_identity={**report["company_identity"], "cik": ""}), "required field"),
        (lambda report: report.update(company_reports={"TD": {}, "PFSI": {}}), "exactly one company"),
        (lambda report: report.update(html=report["html"].replace("</body>", "<div data-brief-company='PFSI' data-brief-cik='0001745916' data-brief-event='2026-Q3'></div></body>")), "exactly one data-brief-company"),
        (lambda report: report.update(text="Company: The Toronto-Dominion Bank (TD)\nEarnings period: Q3 2026\nCompany: PennyMac Financial Services (PFSI)\n"), "exactly one Company"),
    ],
)
def test_strict_boundary_rejects_ambiguous_artifacts(change, expected: str) -> None:
    report = _report()
    change(report)

    with pytest.raises(CompanyBoundaryError, match=expected):
        require_company_boundary(report)


def test_source_documents_reject_mixed_issuer_even_when_period_matches(tmp_path: Path) -> None:
    current = _document(tmp_path, name="q3-release")
    other = _document(tmp_path, name="other", cik="0001745916", ticker="PFSI")
    identity = BriefIdentity("TD", "0000947263", "2026-Q3")

    with pytest.raises(CompanyBoundaryError, match="do not match"):
        validate_source_documents([current, other], identity)


def test_plaintext_header_does_not_accept_a_cik_marker() -> None:
    report = _report(text="Company: The Toronto-Dominion Bank (TD)\nEarnings period: Q3 2026\nCIK: 0000947263")

    with pytest.raises(CompanyBoundaryError, match="keep CIK out"):
        validate_report_surfaces(report)


def test_branded_logo_ticker_must_match_report_identity() -> None:
    report = _report(
        html=(
            "<body data-brief-company='TD' data-brief-cik='0000947263' data-brief-event='2026-Q3'>"
            "<img data-brand-ticker='PFSI' src='https://example.test/logo.png' alt='PFSI'>"
            "</body>"
        )
    )

    with pytest.raises(CompanyBoundaryError, match="branded image ticker"):
        validate_report_surfaces(report)


def test_company_report_child_surface_must_match_outer_identity() -> None:
    outer = _report(
        reports={
            "TD": {
                "html": "<body data-brief-company='PFSI' data-brief-cik='0001745916' data-brief-event='2026-Q3'><p>wrong</p></body>",
                "text": "Company: PennyMac Financial Services (PFSI)\nEarnings period: Q3 2026\n\nwrong",
            }
        }
    )

    with pytest.raises(CompanyBoundaryError, match="HTML identity marker"):
        require_company_boundary(outer)


def test_actual_strict_mime_alternatives_are_bound_to_header() -> None:
    identity = BriefIdentity("TD", "0000947263", "2026-Q3")
    message = delivery.EmailMessage()
    message[IDENTITY_HEADER] = identity.token()
    message[BOUNDARY_HEADER] = BOUNDARY_VERSION
    message.set_content("Company: The Toronto-Dominion Bank (TD)\nEarnings period: Q3 2026\n")
    message.add_alternative("<body data-brief-company='TD' data-brief-cik='0000947263' data-brief-event='2026-Q3'><p>TD</p></body>", subtype="html")

    parsed = BytesParser(policy=policy.default).parsebytes(message.as_bytes())
    assert validate_mime_message(parsed)["identity"]["ticker"] == "TD"

    parsed[BOUNDARY_HEADER] = "wrong"
    with pytest.raises(CompanyBoundaryError, match="valid company-boundary"):
        validate_mime_message(parsed)


def test_prepare_and_delivery_reject_changed_strict_mime_before_smtp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    current = _document(tmp_path, name="q3-release")
    report = _report()
    path = delivery.prepare_messages(
        {"_send": True, "email": {"recipient": "owner@example.com", "from_address": "sender@example.com"}},
        report,
        [current],
        tmp_path / "preview",
    )[0]
    parsed_message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    html_part = next(part for part in parsed_message.walk() if part.get_content_type() == "text/html")
    html_part.set_content(html_part.get_content().replace("data-brief-company='TD'", "data-brief-company='PFSI'"), subtype="html")
    path.write_bytes(parsed_message.as_bytes())
    opened: list[bool] = []
    monkeypatch.setattr(delivery.smtplib, "SMTP_SSL", lambda *a, **k: opened.append(True))
    monkeypatch.setattr(delivery.smtplib, "SMTP", lambda *a, **k: opened.append(True))
    result = delivery.deliver_messages(
        {"_send": True, "email": {"recipient": "owner@example.com", "from_address": "sender@example.com", "smtp_host": "smtp.example", "smtp_username": "sender@example.com", "smtp_password_env": "MISSING"}},
        [path],
        tmp_path / "state.sqlite",
    )

    assert result[0]["status"] == "company_boundary_hold"
    assert opened == []


def test_diagnostic_header_cannot_bypass_normal_delivery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    message = EmailMessage()
    message["Subject"] = "Forged diagnostic message"
    message[TEST_HEADER] = "smtp"
    message.set_content("This message has no verified company identity.")
    path = tmp_path / "forged-diagnostic.eml"
    path.write_bytes(message.as_bytes())

    monkeypatch.setenv("TEST_COMPANY_BOUNDARY_PASSWORD", "fixture-password")
    opened: list[bool] = []
    monkeypatch.setattr(delivery.smtplib, "SMTP_SSL", lambda *args, **kwargs: opened.append(True))
    result = delivery.deliver_messages(
        {
            "_send": True,
            "email": {
                "from_address": "sender@example.com",
                "recipient": "owner@example.com",
                "smtp_host": "smtp.example.test",
                "smtp_port": 465,
                "smtp_security": "ssl",
                "smtp_username": "sender@example.com",
                "smtp_password_env": "TEST_COMPANY_BOUNDARY_PASSWORD",
            },
        },
        [path],
        tmp_path / "state.sqlite",
    )

    assert result[0]["status"] == "company_boundary_hold"
    assert opened == []


def test_send_test_dry_run_uses_explicit_diagnostic_exemption(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[bool] = []
    monkeypatch.setattr(delivery.smtplib, "SMTP_SSL", lambda *args, **kwargs: opened.append(True))
    monkeypatch.setattr(delivery.smtplib, "SMTP", lambda *args, **kwargs: opened.append(True))

    result = delivery.send_test(
        {"_storage": str(tmp_path), "email": {"from_address": "sender@example.com", "recipient": "owner@example.com"}},
        tmp_path / "state.sqlite",
    )

    assert result[0]["status"] == "sending_disabled"
    assert opened == []
    preview = next((tmp_path / "previews").glob("*.eml"))
    parsed = BytesParser(policy=policy.default).parsebytes(preview.read_bytes())
    assert parsed[TEST_HEADER] == "smtp"


@pytest.mark.parametrize("surface", ["html", "plain", "subject"])
@pytest.mark.parametrize("bad_copy", ["Result \u2014 update", "Loss: -$77m"])
def test_display_rule_blocks_tampered_email_before_smtp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surface: str, bad_copy: str) -> None:
    doc = _document(tmp_path, name="release")
    config = {"_send": True, "email": {"recipient": "owner@example.com", "from_address": "sender@example.com", "smtp_host": "smtp.example", "smtp_username": "sender@example.com", "smtp_password_env": "MISSING"}}
    path = delivery.prepare_messages(config, _report(), [doc], tmp_path / "preview")[0]
    message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    if surface == "subject":
        message.replace_header("Subject", bad_copy)
    else:
        part = message.get_body(preferencelist=(surface,))
        part.set_content(part.get_content().replace("TD result.", bad_copy), subtype=surface)
    path.write_bytes(message.as_bytes(policy=policy.SMTP))
    opened = []
    monkeypatch.setattr(delivery.smtplib, "SMTP_SSL", lambda *a, **k: opened.append(True))
    monkeypatch.setattr(delivery.smtplib, "SMTP", lambda *a, **k: opened.append(True))
    result = delivery.deliver_messages(config, [path], tmp_path / "state.sqlite")
    assert result[0]["status"] == "company_boundary_hold"
    assert "P0 reader-display" in result[0]["detail"]
    assert opened == []


def test_preparation_checks_subject_and_appended_document_labels(tmp_path: Path) -> None:
    report = _report()
    report["subject"] = "Result \u2014 update"
    doc = _document(tmp_path, name="release")
    with pytest.raises(ValueError, match="em dash.*subject"):
        delivery.prepare_messages({"email": {}}, report, [doc], tmp_path / "subject")
    report["subject"] = "TD earnings"
    doc.title = "Release \u2014 commentary"
    with pytest.raises(ValueError, match="em dash"):
        delivery.prepare_messages({"email": {}}, report, [doc], tmp_path / "footer")
