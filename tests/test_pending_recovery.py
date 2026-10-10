"""Offline regression tests for durable recovery of pending disclosures.

The source results below model a collector that first records a specific
retry intent and later discovers the archived document.  The reporting and
delivery doubles come from ``test_company_events`` so these tests exercise the
real SQLite state and pipeline selection paths without live sources or SMTP.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from servicing_brief.models import SourceResult
from servicing_brief.pipeline import run
from servicing_brief.state import State
from tests.test_company_events import company_pipeline


def _pending_for(document, *, source_key: str | None = None, url: str | None = None,
                 include_document: bool = True, include_accession: bool = True,
                 issuer: str | None = None, ticker: str | None = None,
                 cik: str | None = None, source: str | None = None) -> dict:
    """Build the collector's durable pending selector for one document."""
    item = {
        "source_key": source_key or f"{document.issuer}:{document.source}",
        "issuer": issuer if issuer is not None else document.issuer,
        "ticker": ticker if ticker is not None else document.issuer,
        "cik": cik if cik is not None else document.cik,
        "source": source if source is not None else document.source,
        "kind": document.kind,
        "url": document.url if url is None else url,
        "period": document.period,
    }
    if include_accession:
        item["accession"] = document.accession
    if include_document:
        item["document"] = document.id
    return item


def _dated(document, published: str, **changes):
    """Keep source publication dates explicit while changing test metadata."""
    return replace(document, published=published, **changes)


def _briefing_periods(calls: dict[str, list], start: int = 0) -> set[str]:
    return {
        call["report"]["company_identity"]["event"]
        for call in calls["reports"][start:]
    }


def _historical_ids(config: dict) -> set[str]:
    state = State(Path(config["_storage"]) / "state.sqlite3")
    try:
        return {row[0] for row in state.db.execute("SELECT id FROM historical_documents")}
    finally:
        state.close()


def test_recovered_old_quarter_and_new_release_make_distinct_drafts_then_dedupe(
    company_pipeline,
):
    config, calls, doc, _set_failure = company_pipeline
    q2_release = _dated(doc("TFC", "q2-release", period="2026-Q2"), "2026-07-29")
    older_q2_deck = _dated(
        doc("TFC", "older-q2-deck", kind="presentation", period="2026-Q2"),
        "2026-07-25",
    )
    pending = _pending_for(older_q2_deck)

    first = run(
        config,
        bootstrap=True,
        collector=lambda *args, **kwargs: SourceResult(
            documents=[q2_release], checked=["TFC"], pending=[pending]
        ),
    )
    assert first["status"] == "prepared"

    q3_release = _dated(doc("TFC", "q3-release", period="2026-Q3"), "2026-10-01")
    recovered_deck = _dated(
        doc("TFC", "older-q2-deck", kind="presentation", period="2026-Q2"),
        "2026-07-25",
    )
    collected = run(
        config,
        collector=lambda *args, **kwargs: SourceResult(
            documents=[q3_release, recovered_deck],
            checked=["TFC"],
            checkpoints={"TFC:ir": "2026-10-09"},
        ),
    )

    assert collected["status"] == "prepared"
    assert _briefing_periods(calls, 1) == {"2026-Q2", "2026-Q3"}
    q2_calls = [
        call for call in calls["reports"][1:]
        if call["report"]["company_identity"]["event"] == "2026-Q2"
    ]
    assert len(q2_calls) == 1
    assert recovered_deck.id in {item.id for item in q2_calls[0]["documents"]}

    state = State(Path(config["_storage"]) / "state.sqlite3")
    try:
        assert state.pending() == []
        assert recovered_deck.id in state.recovered_ids()
    finally:
        state.close()

    report_count = len(calls["reports"])
    attachment_count = len(calls["attachments"])
    unchanged = run(
        config,
        collector=lambda *args, **kwargs: SourceResult(
            documents=[q2_release, q3_release, recovered_deck],
            checked=["TFC"],
            checkpoints={"TFC:ir": "2026-10-10"},
        ),
    )
    assert unchanged["status"] == "no_new_disclosures"
    assert len(calls["reports"]) == report_count
    assert len(calls["attachments"]) == attachment_count


def test_recovery_survives_failed_prepare_sqlite_reopen_and_offline_retry(
    company_pipeline,
):
    config, calls, doc, set_failure = company_pipeline
    q3_release = _dated(doc("TFC", "q3-release", period="2026-Q3"), "2026-10-01")
    older_q2_deck = _dated(
        doc("TFC", "older-q2-deck", kind="presentation", period="2026-Q2"),
        "2026-07-25",
    )
    pending = _pending_for(older_q2_deck)

    assert run(
        config,
        bootstrap=True,
        collector=lambda *args, **kwargs: SourceResult(
            documents=[q3_release], checked=["TFC"], pending=[pending]
        ),
    )["status"] == "prepared"

    set_failure("TFC")
    recovered = _dated(
        doc("TFC", "older-q2-deck", kind="presentation", period="2026-Q2"),
        "2026-07-25",
    )
    # The fixture intentionally raises during preparation.  Depending on
    # whether another package is available, run() either returns a partial
    # result or raises after recording the failed attempt.
    try:
        run(
            config,
            collector=lambda *args, **kwargs: SourceResult(
                documents=[recovered],
                checked=["TFC"],
                checkpoints={"TFC:ir": "2026-10-09"},
            ),
        )
    except OSError:
        pass

    state = State(Path(config["_storage"]) / "state.sqlite3")
    try:
        # A successful source checkpoint clears the transient retry row even
        # when packaging fails.  The matched document ID is the durable intent.
        assert state.pending() == []
        assert recovered.id in state.recovered_ids()
        assert recovered.id in {item.id for item in state.documents()}
    finally:
        state.close()

    set_failure(None)
    retried = run(config, offline=True)
    assert retried["status"] == "prepared"
    q2_attachments = [
        attachment for attachment in calls["attachments"]
        if recovered.id in {item.id for item in attachment["documents"]}
    ]
    assert len(q2_attachments) == 1

    report_count = len(calls["reports"])
    attachment_count = len(calls["attachments"])
    again = run(config, offline=True)
    assert again["status"] == "no_new_disclosures"
    assert len(calls["reports"]) == report_count
    assert len(calls["attachments"]) == attachment_count


@pytest.mark.parametrize(
    ("pending_changes", "recovered_changes"),
    [
        ({"issuer": "PFSI", "ticker": "PFSI", "cik": "0001745916"}, {}),
        ({"source": "sec"}, {}),
        ({"url": "https://issuer.invalid/unrelated-q2-deck", "include_document": False}, {}),
    ],
    ids=["other-issuer", "other-source", "unrelated-url"],
)
def test_unmatched_pending_work_leaves_old_backfill_historical(
    company_pipeline, pending_changes, recovered_changes
):
    config, calls, doc, _set_failure = company_pipeline
    q3_release = _dated(doc("TFC", "q3-release", period="2026-Q3"), "2026-10-01")
    older_q2_deck = _dated(
        doc("TFC", "older-q2-deck", kind="presentation", period="2026-Q2"),
        "2026-07-25",
    )
    pending = _pending_for(older_q2_deck, **pending_changes)

    assert run(
        config,
        bootstrap=True,
        collector=lambda *args, **kwargs: SourceResult(
            documents=[q3_release], checked=["TFC"], pending=[pending]
        ),
    )["status"] == "prepared"

    recovered = replace(older_q2_deck, **recovered_changes)
    response = run(
        config,
        collector=lambda *args, **kwargs: SourceResult(
            documents=[recovered],
            checked=["TFC"],
            checkpoints={"TFC:ir": "2026-10-09"},
        ),
    )

    assert response["status"] == "no_new_disclosures"
    assert _briefing_periods(calls, 1) == set()
    assert recovered.id in _historical_ids(config)
    state = State(Path(config["_storage"]) / "state.sqlite3")
    try:
        assert state.pending() == []
        assert recovered.id not in state.recovered_ids()
    finally:
        state.close()


def test_sec_accession_pending_recovers_with_new_filing_url(company_pipeline):
    config, calls, doc, _set_failure = company_pipeline
    q3_filing = _dated(doc("PFSI", "q3-filing", kind="10-Q", period="2026-Q3"), "2026-10-01")
    older_filing = _dated(doc("PFSI", "older-q2-filing", kind="10-Q", period="2026-Q2"), "2026-07-25")
    pending = _pending_for(
        older_filing,
        source_key="PFSI:sec",
        include_document=False,
        url="",
    )
    assert pending["accession"] == older_filing.accession
    assert "document" not in pending

    assert run(
        config,
        bootstrap=True,
        collector=lambda *args, **kwargs: SourceResult(
            documents=[q3_filing], checked=["PFSI"], pending=[pending]
        ),
    )["status"] == "prepared"

    redirected_url = "https://www.sec.gov/Archives/edgar/data/1745916/redirected.htm"
    recovered = replace(older_filing, url=redirected_url)
    response = run(
        config,
        collector=lambda *args, **kwargs: SourceResult(
            documents=[recovered],
            checked=["PFSI"],
            checkpoints={"PFSI:sec": "2026-10-09"},
        ),
    )

    assert response["status"] == "prepared"
    assert "2026-Q2" in _briefing_periods(calls, 1)
    assert recovered.id not in _historical_ids(config)


def test_sec_exhibit_pending_does_not_promote_unrelated_same_accession_exhibit(
    company_pipeline,
):
    """A URL-scoped SEC retry cannot alert every exhibit in its accession."""
    config, calls, doc, _set_failure = company_pipeline
    q3_filing = _dated(
        doc("PFSI", "q3-filing", kind="10-Q", period="2026-Q3"),
        "2026-10-01",
    )
    accession = "0001745916-26-000123"
    filing_index = (
        "https://www.sec.gov/Archives/edgar/data/1745916/"
        "000174591626000123/0001745916-26-000123-index.html"
    )
    exhibit_one = (
        "https://www.sec.gov/Archives/edgar/data/1745916/"
        "000174591626000123/ex99-1.htm"
    )
    exhibit_two = (
        "https://www.sec.gov/Archives/edgar/data/1745916/"
        "000174591626000123/ex99-2.htm"
    )
    older_exhibit = _dated(
        doc("PFSI", "older-q2-exhibit", kind="presentation", period="2026-Q2"),
        "2026-07-25",
    )
    older_exhibit = replace(
        older_exhibit,
        accession=accession,
        url=exhibit_one,
        discovered_from=filing_index,
        metadata={"ticker": "PFSI", "document": "ex99-1.htm"},
    )
    pending = _pending_for(
        older_exhibit,
        source_key="PFSI:sec",
        url=exhibit_one,
        include_document=False,
    )
    assert pending["url"] == exhibit_one
    assert pending["accession"] == accession
    assert "document" not in pending

    assert run(
        config,
        bootstrap=True,
        collector=lambda *args, **kwargs: SourceResult(
            documents=[q3_filing], checked=["PFSI"], pending=[pending]
        ),
    )["status"] == "prepared"

    unrelated_exhibit = replace(
        older_exhibit,
        url=exhibit_two,
        metadata={"ticker": "PFSI", "document": "ex99-2.htm"},
    )
    response = run(
        config,
        collector=lambda *args, **kwargs: SourceResult(
            documents=[unrelated_exhibit],
            checked=["PFSI"],
            checkpoints={"PFSI:sec": "2026-10-09"},
        ),
    )

    assert response["status"] == "no_new_disclosures"
    assert _briefing_periods(calls, 1) == set()
    assert unrelated_exhibit.id in _historical_ids(config)
    state = State(Path(config["_storage"]) / "state.sqlite3")
    try:
        assert state.pending() == []
        assert unrelated_exhibit.id not in state.recovered_ids()
    finally:
        state.close()


def test_sec_whole_accession_pending_matches_filing_index_provenance(
    company_pipeline,
):
    """An accession-level SEC retry matches a document discovered from its index."""
    config, calls, doc, _set_failure = company_pipeline
    q3_filing = _dated(
        doc("PFSI", "q3-filing", kind="10-Q", period="2026-Q3"),
        "2026-10-01",
    )
    accession = "0001745916-26-000124"
    filing_index = (
        "https://www.sec.gov/Archives/edgar/data/1745916/"
        "000174591626000124/0001745916-26-000124-index.html"
    )
    recovered_url = (
        "https://www.sec.gov/Archives/edgar/data/1745916/"
        "000174591626000124/ex99-2.htm"
    )
    older_exhibit = _dated(
        doc("PFSI", "older-q2-exhibit", kind="presentation", period="2026-Q2"),
        "2026-07-25",
    )
    older_exhibit = replace(
        older_exhibit,
        accession=accession,
        url=recovered_url,
        discovered_from=filing_index,
        metadata={"ticker": "PFSI", "document": "ex99-2.htm"},
    )
    pending = _pending_for(
        older_exhibit,
        source_key="PFSI:sec",
        url=filing_index,
        include_document=False,
    )
    assert pending["url"] == filing_index
    assert pending["accession"] == accession
    assert "document" not in pending

    assert run(
        config,
        bootstrap=True,
        collector=lambda *args, **kwargs: SourceResult(
            documents=[q3_filing], checked=["PFSI"], pending=[pending]
        ),
    )["status"] == "prepared"

    response = run(
        config,
        collector=lambda *args, **kwargs: SourceResult(
            documents=[older_exhibit],
            checked=["PFSI"],
            checkpoints={"PFSI:sec": "2026-10-09"},
        ),
    )

    assert response["status"] == "prepared"
    assert "2026-Q2" in _briefing_periods(calls, 1)
    assert older_exhibit.id not in _historical_ids(config)
    state = State(Path(config["_storage"]) / "state.sqlite3")
    try:
        assert state.pending() == []
        assert older_exhibit.id in state.recovered_ids()
    finally:
        state.close()


def test_ir_redirected_document_matches_pending_discovery_url(company_pipeline):
    config, calls, doc, _set_failure = company_pipeline
    q3_release = _dated(doc("TFC", "q3-release", period="2026-Q3"), "2026-10-01")
    original = _dated(
        doc("TFC", "older-q2-deck", kind="presentation", period="2026-Q2"),
        "2026-07-25",
    )
    pending = _pending_for(original, source_key="TFC:ir")

    assert run(
        config,
        bootstrap=True,
        collector=lambda *args, **kwargs: SourceResult(
            documents=[q3_release], checked=["TFC"], pending=[pending]
        ),
    )["status"] == "prepared"

    redirected = replace(
        original,
        url="https://cdn.issuer.invalid/final-q2-deck.pdf",
        metadata={"ticker": "TFC", "discovery_url": original.url},
    )
    response = run(
        config,
        collector=lambda *args, **kwargs: SourceResult(
            documents=[redirected],
            checked=["TFC"],
            checkpoints={"TFC:ir": "2026-10-09"},
        ),
    )

    assert response["status"] == "prepared"
    assert "2026-Q2" in _briefing_periods(calls, 1)
    assert redirected.id not in _historical_ids(config)


def test_matching_pending_for_already_covered_report_does_not_resend(company_pipeline):
    config, calls, doc, _set_failure = company_pipeline
    q2_release = _dated(doc("TFC", "q2-release", period="2026-Q2"), "2026-07-29")
    pending = _pending_for(q2_release)
    assert run(
        config,
        bootstrap=True,
        collector=lambda *args, **kwargs: SourceResult(
            documents=[q2_release], checked=["TFC"], pending=[pending]
        ),
    )["status"] == "prepared"

    report_count = len(calls["reports"])
    attachment_count = len(calls["attachments"])
    second = run(
        config,
        collector=lambda *args, **kwargs: SourceResult(
            documents=[q2_release],
            checked=["TFC"],
            checkpoints={"TFC:ir": "2026-10-09"},
        ),
    )

    assert second["status"] == "no_new_disclosures"
    assert len(calls["reports"]) == report_count
    assert len(calls["attachments"]) == attachment_count
    state = State(Path(config["_storage"]) / "state.sqlite3")
    try:
        assert state.pending() == []
        assert q2_release.id in state.covered_ids()
        assert q2_release.id in state.recovered_ids()
    finally:
        state.close()
