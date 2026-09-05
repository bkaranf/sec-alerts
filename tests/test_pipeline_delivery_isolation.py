"""Offline regression coverage for per-report preparation and delivery isolation."""

from __future__ import annotations

import json
import logging
import sys
from hashlib import sha256
from pathlib import Path
from types import ModuleType

import pytest

from servicing_brief import delivery
from servicing_brief.config import validate_config
from servicing_brief.models import Document, SourceResult
from servicing_brief.pipeline import run
from servicing_brief.reader_value import inventory_html
from servicing_brief.reader_value_release import text_review_html
from servicing_brief.state import State


class _AcceptingSMTP:
    sent: list[object] = []

    def __init__(self, *args, **kwargs):
        self.args = args

    def ehlo(self):
        return None

    def login(self, username, password):
        assert username == "sender@example.com"
        assert password == "fixture-password"

    def send_message(self, message, from_addr=None, to_addrs=None):
        self.__class__.sent.append(message)
        return {}

    def quit(self):
        return None


def _reader_review(html: str) -> dict:
    items = []
    for block in inventory_html(html):
        utility = block["kind"] in {"metadata", "reference", "heading", "headline"}
        items.append(
            {
                "id": block["id"],
                "verdict": "keep",
                "reader_need": "Readers need this specific mortgage servicing result for quarterly context.",
                "incremental_value": "It adds a concrete source-backed detail about current servicing performance.",
                "inclusion_logic": "Keep this block because it supports the mortgage servicing review.",
                "scope": "mortgage_servicing",
                "priority": "utility" if utility else "primary",
                "relevance_bridge": "",
                "reason": "It is specific, sourced, and useful for this audience.",
                "inventory_text": block["text"],
            }
        )
    return {
        "version": 1,
        "priority": "P0",
        "html_sha256": sha256(html.encode("utf-8")).hexdigest(),
        "reviewer": "synthetic test reviewer",
        "objective": "mortgage_servicing",
        "items": items,
    }


def _write_valid_reader_reviews(config: dict) -> None:
    """Approve both synthetic alternatives through the real reader-value gate."""
    state = State(Path(config["_storage"]) / "state.sqlite3")
    try:
        for row in state.outstanding():
            output = Path(row["output_dir"])
            html = (output / "briefing.html").read_text(encoding="utf-8")
            text = (output / "briefing.txt").read_text(encoding="utf-8")
            (output / "reader-value-review.json").write_text(json.dumps(_reader_review(html), indent=2), encoding="utf-8")
            (output / "reader-value-text-review.json").write_text(
                json.dumps(_reader_review(text_review_html(text)), indent=2), encoding="utf-8"
            )
    finally:
        state.close()


@pytest.fixture
def isolated_delivery_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = validate_config(
        {
            "companies": [
                {"name": "Truist Financial Corporation", "ticker": "TFC", "cik": "92230", "enabled": True},
                {"name": "PennyMac Financial Services, Inc.", "ticker": "PFSI", "cik": "1745916", "enabled": True},
            ],
            "email": {
                "from_address": "sender@example.com",
                "recipient": "owner@example.com",
                "smtp_host": "smtp.example.invalid",
                "smtp_port": 465,
                "smtp_security": "ssl",
                "smtp_username": "sender@example.com",
                "smtp_password_env": "TEST_PIPELINE_PASSWORD",
                "retry_attempts": 1,
            },
        },
        tmp_path,
    )
    reports: list[dict] = []
    reporting_module = ModuleType("servicing_brief.reporting")

    def build(config, documents, **kwargs):
        documents = list(documents)
        ticker = documents[0].issuer
        cik = documents[0].cik
        period = documents[0].period
        text_period = f"Q{period[-1]} {period[:4]}" if period.endswith(tuple(f"Q{i}" for i in range(1, 5))) else period.replace("-", " ")
        html = f"<body data-brief-company='{ticker}' data-brief-cik='{cik}' data-brief-event='{period}'><p>{ticker} mortgage servicing earnings improved during the quarter.</p></body>"
        text = f"Company: {ticker} ({ticker})\nEarnings period: {text_period}\n\n{ticker} mortgage servicing earnings improved during the quarter."
        report = {
            "subject": f"{ticker} earnings",
            "html": html,
            "text": text,
            # Keep the synthetic MIME Date stable across the two runs in the
            # retry assertion; production reports provide generated_at too.
            "generated_at": "2026-07-29T12:00:00+00:00",
            "company_identity": {"ticker": ticker, "cik": cik, "event": period, "kind": "earnings_brief"},
            "company_reports": {ticker: {"html": html, "text": text}},
            "evidence": [],
        }
        reports.append({"ticker": ticker, "report": report})
        return report

    reporting_module.build_report = build
    monkeypatch.setitem(sys.modules, "servicing_brief.reporting", reporting_module)
    monkeypatch.setenv("TEST_PIPELINE_PASSWORD", "fixture-password")
    monkeypatch.setattr(delivery.smtplib, "SMTP_SSL", _AcceptingSMTP)

    def document(ticker: str) -> Document:
        ciks = {"TFC": "0000092230", "PFSI": "0001745916"}
        content = f"{ticker} offline earnings release".encode()
        path = tmp_path / f"{ticker}.html"
        path.write_bytes(content)
        return Document(
            issuer=ticker,
            cik=ciks[ticker],
            title=f"{ticker} earnings",
            kind="release",
            source="ir",
            url=f"https://example.invalid/{ticker}/release",
            published="2026-07-29",
            period="2026-Q2",
            path=str(path),
            content_hash=sha256(content).hexdigest(),
            metadata={"ticker": ticker},
        )

    return config, document, reports


def test_send_isolates_preparation_exception_and_accepts_later_company(
    isolated_delivery_pipeline,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    config, document, reports = isolated_delivery_pipeline
    tfc, pfsi = document("TFC"), document("PFSI")
    collect = lambda *args, **kwargs: SourceResult(documents=[tfc, pfsi], checked=["TFC", "PFSI"])

    # Establish two durable previews first.  The second invocation exercises
    # only the outstanding send loop because the source bytes are unchanged.
    initial = run(config, bootstrap=True, collector=collect)
    assert initial["status"] == "prepared"
    _write_valid_reader_reviews(config)

    original_prepare = delivery.prepare_messages
    send_phase = False
    failed_once = False

    def prepare_with_one_failure(config, report, documents, output_dir):
        nonlocal failed_once
        if send_phase and not failed_once:
            failed_once = True
            raise RuntimeError("fixture SMTP credential SUPER_SECRET must not escape")
        return original_prepare(config, report, documents, output_dir)

    monkeypatch.setattr(delivery, "prepare_messages", prepare_with_one_failure)
    _AcceptingSMTP.sent = []
    send_phase = True
    with caplog.at_level(logging.ERROR, logger="servicing_brief.pipeline"):
        response = run(config, send=True, collector=collect)

    assert response["status"] == "delivery_incomplete"
    assert len(response["delivery_errors"]) == 1
    issue = response["delivery_errors"][0]
    assert issue["source"] == "delivery"
    assert issue["phase"] == "preparation"
    assert issue["error_type"] == "RuntimeError"
    assert "SUPER_SECRET" not in caplog.text
    assert "SUPER_SECRET" not in json.dumps(response)
    assert len(_AcceptingSMTP.sent) == 1

    state = State(Path(config["_storage"]) / "state.sqlite3")
    briefing_statuses = {row["status"] for row in state.status()["briefings"]}
    assert briefing_statuses == {"prepared", "accepted"}
    state.close()
    assert {entry["ticker"] for entry in reports} == {"TFC", "PFSI"}


def test_new_preparation_failure_does_not_block_older_outstanding_send(
    isolated_delivery_pipeline,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    config, document, _reports = isolated_delivery_pipeline
    older = document("PFSI")
    newer = document("TFC")

    # Leave one authorized, durable draft waiting for delivery.
    assert run(
        config,
        bootstrap=True,
        collector=lambda *args, **kwargs: SourceResult(documents=[older], checked=["PFSI"]),
    )["status"] == "prepared"
    _write_valid_reader_reviews(config)

    original_prepare = delivery.prepare_messages

    def fail_new_company(config, report, documents, output_dir):
        if list(documents)[0].issuer == "TFC":
            raise RuntimeError("new preparation failed with SUPER_SECRET")
        return original_prepare(config, report, documents, output_dir)

    monkeypatch.setattr(delivery, "prepare_messages", fail_new_company)
    _AcceptingSMTP.sent = []
    with caplog.at_level(logging.ERROR, logger="servicing_brief.pipeline"):
        response = run(
            config,
            send=True,
            collector=lambda *args, **kwargs: SourceResult(
                documents=[older, newer], checked=["TFC", "PFSI"]
            ),
        )

    # The new TFC package failed before it could be saved, but the older PFSI
    # preview still reached the fake provider.  Preparation failure remains
    # visible in the aggregate status for the run.
    assert response["status"] == "partial_failure"
    assert len(response["preparation_errors"]) == 1
    assert response["preparation_errors"][0]["cik"] == "0000092230"
    assert [item["status"] for item in response["delivery"]] == ["accepted"]
    assert len(_AcceptingSMTP.sent) == 1
    assert "SUPER_SECRET" not in caplog.text

    state = State(Path(config["_storage"]) / "state.sqlite3")
    briefings = state.status()["briefings"]
    assert len(briefings) == 1 and briefings[0]["status"] == "accepted"
    state.close()


@pytest.mark.parametrize("failure_kind", ["delivery", "bookkeeping"])
def test_send_loop_isolates_unexpected_delivery_and_bookkeeping_exceptions(
    isolated_delivery_pipeline,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_kind: str,
):
    config, document, _reports = isolated_delivery_pipeline
    tfc, pfsi = document("TFC"), document("PFSI")
    collect = lambda *args, **kwargs: SourceResult(
        documents=[tfc, pfsi], checked=["TFC", "PFSI"]
    )
    assert run(config, bootstrap=True, collector=collect)["status"] == "prepared"
    _write_valid_reader_reviews(config)

    state = State(Path(config["_storage"]) / "state.sqlite3")
    first_report_id = state.outstanding()[0]["id"]
    state.close()
    original_deliver = delivery.deliver_messages
    original_set_status = State.set_report_status
    failed_once = False

    if failure_kind == "delivery":

        def fail_first_delivery(config, paths, state_db):
            nonlocal failed_once
            if not failed_once:
                failed_once = True
                raise RuntimeError("delivery provider payload SUPER_SECRET")
            return original_deliver(config, paths, state_db)

        monkeypatch.setattr(delivery, "deliver_messages", fail_first_delivery)
        expected_first_run_sends = 1
    else:

        def fail_first_bookkeeping(state, report_id, status):
            nonlocal failed_once
            if not failed_once and report_id == first_report_id:
                failed_once = True
                raise RuntimeError("bookkeeping payload SUPER_SECRET")
            return original_set_status(state, report_id, status)

        monkeypatch.setattr(State, "set_report_status", fail_first_bookkeeping)
        expected_first_run_sends = 2

    _AcceptingSMTP.sent = []
    with caplog.at_level(logging.ERROR, logger="servicing_brief.pipeline"):
        first = run(config, send=True, collector=collect)

    assert first["status"] == "delivery_incomplete"
    assert len(first["delivery_errors"]) == 1
    assert first["delivery_errors"][0]["phase"] == "delivery"
    assert first["delivery_errors"][0]["error_type"] == "RuntimeError"
    assert "SUPER_SECRET" not in caplog.text
    assert len(_AcceptingSMTP.sent) == expected_first_run_sends
    assert any(item["status"] == "accepted" for item in first["delivery"])

    state = State(Path(config["_storage"]) / "state.sqlite3")
    assert {row["status"] for row in state.status()["briefings"]} == {"prepared", "accepted"}
    state.close()

    # The failed report is retried.  In the bookkeeping case the provider
    # already accepted its message, so the retry is acknowledged locally and
    # does not call SMTP again.  In the delivery case only that report is sent.
    second = run(config, send=True, collector=collect)
    assert second["status"] == "provider_accepted"
    assert second["delivery_errors"] == []
    assert len(_AcceptingSMTP.sent) == 2
    if failure_kind == "bookkeeping":
        assert second["delivery"][0]["status"] == "already_accepted"

    state = State(Path(config["_storage"]) / "state.sqlite3")
    assert {row["status"] for row in state.status()["briefings"]} == {"accepted"}
    state.close()
