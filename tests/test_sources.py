"""Offline source-layer tests; live SEC/IR smoke is intentionally separate."""

from __future__ import annotations

import contextlib
import sys
import types
from hashlib import sha256
from pathlib import Path

import pytest
from filelock import FileLock

from servicing_brief.models import SourceResult
from servicing_brief.sources import collector as collector_module
from servicing_brief.sources import ir as ir_module
from servicing_brief.sources import ratelimit as ratelimit_module
from servicing_brief.sources import sec as sec_module


class FakeAttachment:
    def __init__(self, document, description, payload, *, sequence="2", document_type="EX-99.1", url="https://www.sec.gov/doc", purpose="", ixbrl=False):
        self.document = document
        self.description = description
        self.document_type = document_type
        self.sequence_number = sequence
        self.url = url
        self.purpose = purpose
        self.ixbrl = ixbrl
        self._payload = payload

    def download(self):
        return self._payload


class FakeFiling:
    def __init__(self, form, accession, filing_date, report_date, attachments, *, items=""):
        self.form = form
        self.accession_no = accession
        self.filing_date = filing_date
        self.report_date = report_date
        self.company = "FAKE ISSUER"
        self.items = items
        self.attachments = attachments
        self.filing_url = f"https://www.sec.gov/Archives/{accession}"
        self.acceptance_datetime = f"{filing_date}T12:00:00.000Z"

    def text(self):
        return "quarterly earnings results and mortgage servicing portfolio"


@pytest.fixture
def source_config(tmp_path):
    return {
        "_storage": str(tmp_path),
        "sources": {"lookback_days": 120, "overlap_days": 14, "bootstrap_lookback_days": 450, "max_relevant_8k_per_company": 10},
        "companies": [{
            "ticker": "TFC",
            "name": "Truist Financial Corporation",
            "cik": "0000092230",
            "enabled": True,
            "ir_url": "https://ir.example.test/earnings",
            "ir_pages": ["https://ir.example.test/earnings"],
        }, {
            "ticker": "PFSI",
            "name": "PennyMac Financial Services, Inc.",
            "cik": "0001745916",
            "enabled": True,
            "ir_url": "https://pfsi.example.test/earnings",
            "ir_pages": ["https://pfsi.example.test/earnings"],
        }],
    }


def test_sec_selects_relevant_8k_and_preserves_furnished_metadata(source_config, monkeypatch):
    release = FakeAttachment("earnings.html", "Earnings release", b"Q2 earnings", sequence="2", document_type="EX-99.1")
    primary = FakeAttachment("primary.htm", "Current report", "ITEM 2.02\nITEM 9.01", sequence="1", document_type="HTML")
    relevant = FakeFiling("8-K", "0000092230-26-000001", "2026-07-17", "", [primary, release], items="2.02,9.01")
    routine = FakeFiling("8-K", "0000092230-26-000002", "2026-07-18", "", [FakeAttachment("admin.htm", "Director appointment", b"routine", sequence="1", document_type="HTML")], items="5.02")
    quarterly = FakeFiling("10-Q", "0000092230-26-000003", "2026-07-20", "2026-06-30", [FakeAttachment("tfc-q2.htm", "Quarterly report", "Q2 2026", sequence="1", document_type="HTML")])

    class FakeCompany:
        not_found = False

        def __init__(self, cik):
            self.cik = int(cik)

        def get_filings(self, **kwargs):
            assert kwargs["form"]
            return [relevant, routine, quarterly]

    fake_edgar = types.ModuleType("edgar")
    fake_edgar.Company = FakeCompany
    monkeypatch.setitem(sys.modules, "edgar", fake_edgar)

    @contextlib.contextmanager
    def fake_guard(*args, **kwargs):
        yield {"acquired": True, "manager_verified": True}

    monkeypatch.setattr(sec_module, "sec_acquisition_guard", fake_guard)
    monkeypatch.setenv("EDGAR_IDENTITY", "configured-for-test")

    result = sec_module.discover_sec(source_config, source_config["companies"][0], bootstrap=True)
    assert result.errors == []
    assert result.documents
    assert all(document.cik == "0000092230" for document in result.documents)
    assert any(document.classification == "sec-furnished" for document in result.documents)
    assert all(Path(document.path).is_file() for document in result.documents)
    assert {document.period for document in result.documents} == {"2026-Q2", "unknown"}
    assert result.checkpoints == {"TFC:sec": result.checkpoints["TFC:sec"]}


def test_sec_missing_identity_is_explicit_and_does_not_prompt(source_config, monkeypatch):
    monkeypatch.delenv("EDGAR_IDENTITY", raising=False)
    result = sec_module.discover_sec(source_config, source_config["companies"][0])
    assert result.documents == []
    assert result.errors[0]["credentials"] == "configured=no"
    assert "configured-for-test" not in str(result.errors)


def test_ir_discovery_preserves_official_to_cdn_relationship(source_config, monkeypatch):
    page_url = "https://ir.example.test/earnings"
    cdn_url = "https://cdn.example.test/docs/PFSI-Q2-2026-Earnings-Presentation.pdf"

    class Response:
        def __init__(self, status_code, content=b"", headers=None, url=None):
            self.status_code = status_code
            self.content = content
            self.headers = headers or {"content-type": "text/html"}
            self.url = url or page_url
            self.text = content.decode("utf-8", errors="ignore")

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, **kwargs):
            if url.endswith("/robots.txt"):
                return Response(404, b"", {"content-type": "text/plain"}, url)
            if "cdn.example" in url:
                return Response(200, b"%PDF-fake-presentation", {"content-type": "application/pdf"}, url)
            page = f'<html><a href="{cdn_url}">Q2 2026 Earnings Presentation</a></html>'.encode()
            return Response(200, page, {"content-type": "text/html", "etag": '"page1"'}, url)

    monkeypatch.setattr(ir_module.httpx, "Client", Client)
    company = {**source_config["companies"][1], "ir_url": page_url, "ir_pages": [page_url]}
    result = ir_module.discover_ir(source_config, company, bootstrap=True)
    assert result.errors == []
    assert len(result.documents) == 1
    document = result.documents[0]
    assert document.source == "ir"
    assert document.classification == "issuer-published"
    assert document.period == "2026-Q2"
    assert document.discovered_from == page_url
    assert document.metadata["cdn_discovered_from_official_page"] is True
    assert Path(document.path).read_bytes() == b"%PDF-fake-presentation"


def test_ir_discovery_crawls_extensionless_navigation_and_keeps_material_html_pdf(source_config, monkeypatch):
    """Navigation routes are followed while release HTML and PDFs stay documents."""

    root_url = "https://ir.example.test/investors/"
    events_url = f"{root_url}events-and-presentations"
    annual_url = f"{root_url}annual-reports"
    detail_url = f"{root_url}events-and-presentations.aspx?item=90"
    release_url = f"{root_url}events-and-presentations/Q2-2026-earnings-release.html"
    deck_url = f"{root_url}events-and-presentations/Q2-2026-earnings-presentation.pdf"
    annual_pdf_url = f"{root_url}reports/PFSI-2025-Annual-Report.pdf"
    transcript_url = f"{root_url}docs/Q2-2026-Earnings-Call-Transcript.pdf"
    requested: list[str] = []

    pages = {
        root_url: (
            '<a href="events-and-presentations">Events &amp; Presentations</a>'
            '<a href="annual-reports">Annual Reports</a>'
            '<a href="events-and-presentations.aspx?item=90">Event detail</a>'
        ).encode(),
        events_url: (
            f'<a href="{release_url}">Q2 2026 Earnings Release</a>'
            f'<a href="{deck_url}">Q2 2026 Earnings Presentation</a>'
        ).encode(),
        annual_url: f'<a href="{annual_pdf_url}">FY2025 Annual Report</a>'.encode(),
        detail_url: f'<a href="{transcript_url}">Q2 2026 Earnings Call Transcript</a>'.encode(),
        release_url: b"<html><title>Q2 2026 Earnings Release</title><p>Servicing results.</p></html>",
    }
    binaries = {
        deck_url: b"%PDF-presentation",
        annual_pdf_url: b"%PDF-annual-report",
        transcript_url: b"%PDF-transcript",
    }

    class Response:
        def __init__(self, status_code, content=b"", headers=None, url=None):
            self.status_code = status_code
            self.content = content
            self.headers = headers or {"content-type": "text/html"}
            self.url = url or root_url
            self.text = content.decode("utf-8", errors="ignore")

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, **kwargs):
            requested.append(url)
            if url.endswith("/robots.txt"):
                return Response(404, b"", {"content-type": "text/plain"}, url)
            if url in binaries:
                return Response(200, binaries[url], {"content-type": "application/pdf"}, url)
            if url in pages:
                return Response(200, pages[url], {"content-type": "text/html"}, url)
            return Response(404, b"", {"content-type": "text/html"}, url)

    monkeypatch.setattr(ir_module.httpx, "Client", Client)
    company = {
        **source_config["companies"][1],
        "ir_url": root_url,
        "ir_pages": [root_url],
    }

    result = ir_module.discover_ir(source_config, company, bootstrap=True)

    assert result.errors == []
    assert result.pending == []
    assert {events_url, annual_url, detail_url} <= set(requested)
    assert len(result.documents) == 4
    by_url = {document.metadata["discovery_url"]: document for document in result.documents}
    assert by_url[release_url].kind == "release"
    assert by_url[release_url].period == "2026-Q2"
    assert by_url[deck_url].kind == "presentation"
    assert by_url[annual_pdf_url].kind == "annual-report"
    assert by_url[annual_pdf_url].period == "2025-FY"
    assert by_url[transcript_url].kind == "transcript"


def test_ir_robots_disallow_is_reported_as_blocked(source_config, monkeypatch):
    class Response:
        status_code = 200
        headers = {"content-type": "text/plain"}
        url = "https://ir.example.test/robots.txt"
        text = "User-agent: *\nDisallow: /earnings\n"
        content = text.encode()

    class Client:
        def get(self, url, **kwargs):
            return Response()

    client = Client()
    cache = {}
    allowed, reason = ir_module._robots_allowed(source_config, client, cache, "https://ir.example.test/earnings")
    assert allowed is False and "disallow" in reason


def test_collector_isolates_issuer_source_failures(source_config, monkeypatch):
    calls = []

    def sec(config, company, **kwargs):
        calls.append(("sec", company["ticker"]))
        if company["ticker"] == "TFC":
            raise RuntimeError("blocked")
        return SourceResult(checked=[company["ticker"]], checkpoints={f"{company['ticker']}:sec": "now"})

    def ir(config, company, **kwargs):
        calls.append(("ir", company["ticker"]))
        return SourceResult(checked=[company["ticker"]], checkpoints={f"{company['ticker']}:ir": "now"})

    monkeypatch.setattr(collector_module, "discover_sec", sec)
    monkeypatch.setattr(collector_module, "discover_ir", ir)
    result = collector_module.collect(source_config)
    assert calls == [("sec", "TFC"), ("ir", "TFC"), ("sec", "PFSI"), ("ir", "PFSI")]
    assert {"TFC", "PFSI"}.issubset(result.checked)
    assert result.errors and result.errors[0]["source"] == "sec"


def test_period_inference_does_not_use_publication_dates():
    assert sec_module.infer_period("Published July 17, 2026; compares Q2 2025 with Q2 2026", form="8-K") == "2026-Q2"
    assert sec_module.infer_period("Published July 17, 2026; incorporated in Delaware in 2018", form="8-K") == "unknown"


def test_sec_filters_xbrl_rows_and_groups_8k_material_by_earnings_period(source_config, monkeypatch):
    primary = FakeAttachment(
        "current.htm",
        "FORM 8-K",
        "<html><body>Current report; July 29, 2026</body></html>",
        sequence="1",
        document_type="HTML",
    )
    release = FakeAttachment(
        "ex99-1.htm",
        "EXHIBIT 99.1",
        "<html><b>PennyMac Financial Services, Inc. Reports Second Quarter 2026 Results</b></html>",
        sequence="2",
        document_type="EX-99.1",
    )
    deck = FakeAttachment(
        "ex99-2.htm",
        "EXHIBIT 99.2",
        "<html><b>2Q26 Earnings Report</b><img src='slide01.jpg'></html>",
        sequence="3",
        document_type="EX-99.2",
    )
    image = FakeAttachment("slide01.jpg", "GRAPHIC", b"JPEG", sequence="4", document_type="GRAPHIC")
    xbrl = FakeAttachment("R1.htm", "IDEA: XBRL DOCUMENT", b"xbrl", sequence="5", document_type="HTML", ixbrl=True)
    filing = FakeFiling("8-K", "0001745916-26-000001", "2026-07-29", "2026-07-29", [primary, release, deck, image, xbrl], items="2.02,9.01")

    class FakeCompany:
        not_found = False

        def __init__(self, cik):
            self.cik = int(cik)

        def get_filings(self, **kwargs):
            return [filing]

    fake_edgar = types.ModuleType("edgar")
    fake_edgar.Company = FakeCompany
    monkeypatch.setitem(sys.modules, "edgar", fake_edgar)

    @contextlib.contextmanager
    def fake_guard(*args, **kwargs):
        yield {"acquired": True, "manager_verified": True}

    monkeypatch.setattr(sec_module, "sec_acquisition_guard", fake_guard)
    monkeypatch.setenv("EDGAR_IDENTITY", "configured-for-test")
    result = sec_module.discover_sec(source_config, source_config["companies"][1], bootstrap=True)

    assert result.errors == []
    assert len(result.documents) == 3
    assert {doc.period for doc in result.documents} == {"2026-Q2"}
    assert {doc.kind for doc in result.documents} == {"8-K", "release", "presentation"}
    deck_doc = next(doc for doc in result.documents if doc.kind == "presentation")
    assert len(deck_doc.metadata["assets"]) == 1
    assert Path(deck_doc.metadata["assets"][0]["path"]).read_bytes() == b"JPEG"


def test_sec_xbrl_description_is_filtered_even_when_html(source_config):
    xbrl = FakeAttachment("R12.htm", "IDEA: XBRL DOCUMENT", b"x", document_type="HTML", ixbrl=False)
    assert sec_module._attachment_is_xbrl(xbrl) is True
    assert sec_module._attachment_is_relevant(xbrl, "10-Q", is_eight_k=False) is False


def test_sec_drops_board_appointment_exhibit_even_with_furnished_item(source_config, monkeypatch):
    primary = FakeAttachment(
        "current.htm",
        "FORM 8-K",
        "<html><body>Current report</body></html>",
        sequence="1",
        document_type="HTML",
    )
    board = FakeAttachment(
        "ex99-1.htm",
        "Additional exhibit",
        "<html><b>Company Appoints Sarah Watterson as Independent Board Director</b>"
        "<p>She brings financial services and mortgage origination experience.</p></html>",
        sequence="2",
        document_type="EX-99.1",
    )
    filing = FakeFiling(
        "8-K",
        "0000092230-26-000099",
        "2026-08-17",
        "",
        [primary, board],
        items="7.01,9.01",
    )

    class FakeCompany:
        not_found = False

        def __init__(self, cik):
            self.cik = int(cik)

        def get_filings(self, **kwargs):
            return [filing]

    fake_edgar = types.ModuleType("edgar")
    fake_edgar.Company = FakeCompany
    monkeypatch.setitem(sys.modules, "edgar", fake_edgar)

    @contextlib.contextmanager
    def fake_guard(*args, **kwargs):
        yield {"acquired": True, "manager_verified": True}

    monkeypatch.setattr(sec_module, "sec_acquisition_guard", fake_guard)
    monkeypatch.setenv("EDGAR_IDENTITY", "configured-for-test")
    result = sec_module.discover_sec(source_config, source_config["companies"][0], bootstrap=True)

    assert result.documents == []


def test_wfc_compact_deck_period_uses_opening_and_avoids_comparison_conflict(source_config, monkeypatch):
    """A 2Q26 deck can mention 3Q25 in later footnotes; that is not a conflict."""

    primary = FakeAttachment(
        "wfc-20260714.htm",
        "Current report",
        "<html><body>Current report</body></html>",
        sequence="1",
        document_type="HTML",
    )
    release = FakeAttachment(
        "wfc2qer07-14x26ex991xrelea.htm",
        "Document",
        "<html><h1>Wells Fargo Reports Second Quarter 2026 Results</h1></html>",
        sequence="2",
        document_type="EX-99.1",
    )
    deck = FakeAttachment(
        "ex993-wellsfargo2q26pres.htm",
        "Document",
        "<html><body><b>2Q26 Financial Results</b> "
        "<p>Comparisons include the 3Q25 transfer of certain loans.</p></body></html>",
        sequence="3",
        document_type="EX-99.3",
    )
    filing = FakeFiling(
        "8-K",
        "0000072971-26-000288",
        "2026-07-14",
        "",
        [primary, release, deck],
        items="2.02,7.01,9.01",
    )

    class FakeCompany:
        not_found = False

        def __init__(self, cik):
            self.cik = int(cik)

        def get_filings(self, **kwargs):
            return [filing]

    fake_edgar = types.ModuleType("edgar")
    fake_edgar.Company = FakeCompany
    monkeypatch.setitem(sys.modules, "edgar", fake_edgar)

    @contextlib.contextmanager
    def fake_guard(*args, **kwargs):
        yield {"acquired": True, "manager_verified": True}

    monkeypatch.setattr(sec_module, "sec_acquisition_guard", fake_guard)
    monkeypatch.setenv("EDGAR_IDENTITY", "configured-for-test")
    result = sec_module.discover_sec(source_config, source_config["companies"][0], bootstrap=True)

    assert result.errors == []
    assert result.pending == []
    assert len(result.documents) == 3
    assert {document.period for document in result.documents} == {"2026-Q2"}
    assert next(document for document in result.documents if document.metadata["document"].startswith("ex993")).kind == "presentation"


def test_rkt_earnings_exhibit_is_material_perioded_and_classified_from_heading(source_config):
    release = FakeAttachment(
        "rkt-063020268xkex991earnin.htm",
        "Additional exhibit",
        "<html><h1>Rocket Companies Announces Second Quarter 2026 Results</h1>"
        "<p>Mortgage servicing platform results.</p></html>",
        sequence="2",
        document_type="EX-99.1",
    )
    payload = sec_module.file_bytes(release._payload)
    assert sec_module._eight_k_material_attachment(release, payload, ".htm") is True
    assert sec_module._attachment_period_hint(release, payload, ".htm", form="8-K") == "2026-Q2"

    filing = FakeFiling(
        "8-K",
        "0001805284-26-000001",
        "2026-07-30",
        "",
        [release],
        items="2.02,9.01",
    )
    result = SourceResult()
    company = {
        "ticker": "RKT",
        "name": "Rocket Companies, Inc.",
        "cik": "0001805284",
    }
    sec_module._collect_filing(source_config, company, filing, reason="test earnings", result=result)
    assert result.errors == []
    assert len(result.documents) == 1
    assert result.documents[0].period == "2026-Q2"
    assert result.documents[0].kind == "release"


def test_rkt_archived_exhibit_uses_visible_release_heading_without_exhibit_type(source_config):
    """The archived Workiva body establishes release kind without EX-99 metadata."""

    repo = Path(__file__).resolve().parents[1]
    payload = (repo / "tests" / "fixtures" / "rkt_q2_2026_release.html").read_bytes()
    assert sha256(payload).hexdigest() == "50cc793a2716bbfaf9fccd829e4b153ce73dc111fdb5f80f463241e56c389747"

    # Keep the real row's EX-99.1 type so the bounded SEC attachment filter
    # considers it.  The filename and description are generic; release kind
    # must come from the archived issuer heading/body classifier.
    release = FakeAttachment(
        "attachment.htm",
        "Additional exhibit",
        payload,
        sequence="2",
        document_type="EX-99.1",
    )
    filing = FakeFiling(
        "8-K",
        "0001805284-26-000082",
        "2026-08-06",
        "",
        [release],
        items="2.02,7.01,9.01",
    )
    result = SourceResult()
    company = {"ticker": "RKT", "name": "Rocket Companies, Inc.", "cik": "0001805284"}

    sec_module._collect_filing(source_config, company, filing, reason="archived RKT repro", result=result)

    assert result.errors == []
    assert len(result.documents) == 1
    document = result.documents[0]
    assert document.period == "2026-Q2"
    assert document.kind == "release"
    assert document.title == "Rocket Companies Announces Second Quarter 2026 Results"


def test_sec_bounded_candidates_keeps_latest_annual_context_without_old_quarter_flood(source_config):
    def candidate(form, accession, filing_date, report_date, description="Quarterly report"):
        attachment = FakeAttachment(
            f"{accession}.htm",
            description,
            "source",
            sequence="1",
            document_type="HTML",
        )
        return FakeFiling(form, accession, filing_date, report_date, [attachment])

    candidates = [
        candidate("10-Q", "q2-2026", "2026-08-01", "2026-06-30"),
        candidate("10-Q", "q1-2026", "2026-05-01", "2026-03-31"),
        candidate("10-Q", "q4-2025", "2026-02-01", "2025-12-31"),
        candidate("10-K", "k-2025", "2026-02-20", "2025-12-31", "Annual report"),
        candidate("10-Q", "q2-2025", "2025-08-01", "2025-06-30"),
        candidate("10-K", "k-2024", "2025-02-20", "2024-12-31", "Annual report"),
        candidate("10-Q", "q4-2024", "2025-02-01", "2024-12-31"),
    ]

    selected = sec_module._bounded_candidates(source_config, candidates)
    accessions = {sec_module._filing_accession(filing) for filing in selected}

    assert {"q2-2026", "q1-2026", "q2-2025", "k-2025"} <= accessions
    assert {"q4-2025", "k-2024", "q4-2024"}.isdisjoint(accessions)


def test_ir_old_link_period_survives_cdn_redirect_and_deck_kind(source_config):
    old_url = "https://ir.example.test/download/TFC%202Q25%20Earnings%20Deck--LMtest.pdf"
    current_cdn = "https://cdn.example.test/download/current-earnings-deck.pdf"
    fetched = ir_module._Fetched(
        url=old_url,
        final_url=current_cdn,
        status_code=200,
        content=b"%PDF-issuer-material",
        headers={"content-type": "application/pdf"},
    )
    company = {**source_config["companies"][0], "ticker": "TFC"}
    document = ir_module._collect_document(
        {**source_config, "_storage": str(Path(source_config["_storage"]) / "ir-period")},
        company,
        fetched,
        "Earnings release",
        "https://ir.example.test/earnings",
    )
    assert document.period == "2025-Q2"
    assert document.kind == "presentation"
    assert document.metadata["discovery_url"] == old_url


def test_transcript_and_prepared_remarks_kind_precede_generic_earnings_title():
    assert ir_module.title_kind("Truist Q2 2026 Earnings Call Transcript", "TFC_2Q26_Earnings_Call_Transcript.pdf") == "transcript"
    assert ir_module.title_kind("Truist Q2 2026 Earnings Call Prepared Remarks", "TFC_2Q26_prepared-remarks.pdf") == "prepared_remarks"


def test_late_transcript_link_keeps_current_period_and_is_bounded(source_config):
    """A transcript discovered alongside the release remains a current event."""

    links = [
        ("https://ir.example.test/TFC_2Q26_Earnings_Release.pdf", "Earnings release"),
        ("https://ir.example.test/TFC_2Q26_Earnings_Call_Transcript.pdf", "Q2 2026 Earnings Call Transcript"),
        ("https://ir.example.test/TFC_2Q25_Earnings_Call_Transcript.pdf", "Q2 2025 Earnings Call Transcript"),
        ("https://ir.example.test/TFC_2Q24_Earnings_Call_Transcript.pdf", "Q2 2024 Earnings Call Transcript"),
    ]
    selected = ir_module._bound_document_links(source_config, links)
    selected_urls = {url for url, _title in selected}
    assert links[1][0] in selected_urls
    assert links[2][0] in selected_urls
    assert links[3][0] not in selected_urls

    fetched = ir_module._Fetched(
        url=links[1][0],
        final_url="https://cdn.example.test/download/current-transcript.pdf",
        status_code=200,
        content=b"%PDF-transcript",
        headers={"content-type": "application/pdf"},
    )
    document = ir_module._collect_document(
        {**source_config, "_storage": str(Path(source_config["_storage"]) / "ir-transcript")},
        source_config["companies"][0],
        fetched,
        links[1][1],
        source_config["companies"][0]["ir_url"],
    )
    assert document.period == "2026-Q2"
    assert document.kind == "transcript"
    assert document.metadata["discovery_url"] == links[1][0]


def test_ir_transport_dates_are_not_published_dates(source_config):
    """HTTP cache timestamps stay provenance metadata, never issuer timing."""

    fetched = ir_module._Fetched(
        url="https://ir.example.test/TFC_2Q26_Earnings_Release.pdf",
        final_url="https://cdn.example.test/TFC_2Q26_Earnings_Release.pdf",
        status_code=200,
        content=b"%PDF-issuer-material",
        headers={
            "content-type": "application/pdf",
            "last-modified": "Thu, 16 Jul 2026 22:17:16 GMT",
            "date": "Fri, 04 Sep 2026 01:00:00 GMT",
        },
    )
    document = ir_module._collect_document(
        {**source_config, "_storage": str(Path(source_config["_storage"]) / "ir-dates")},
        {**source_config["companies"][0], "ticker": "TFC"},
        fetched,
        "Earnings release",
        "https://ir.example.test/earnings",
    )
    assert document.published == ""
    assert document.metadata["publication_date_source"] == "not_established"
    assert document.metadata["http_last_modified"] == "Thu, 16 Jul 2026 22:17:16 GMT"
    assert document.metadata["http_date"] == "Fri, 04 Sep 2026 01:00:00 GMT"


def test_ir_index_selection_keeps_latest_prior_and_same_quarter_prior_year(source_config):
    links = [
        ("https://ir.example.test/TFC_2Q26_Earnings_Release.pdf", "Earnings release"),
        ("https://ir.example.test/TFC_2Q25_Earnings_Release.pdf", "Earnings release"),
        ("https://ir.example.test/TFC_2Q25_Earnings_Deck--LMtest.pdf", "Earnings release"),
        ("https://ir.example.test/TFC_FY2025_Annual_Report.pdf", "FY2025 Annual Report"),
        ("https://ir.example.test/TFC_FY2024_Annual_Report.pdf", "FY2024 Annual Report"),
        ("https://ir.example.test/TFC_2Q24_Earnings_Release.pdf", "Earnings release"),
    ]
    selected = ir_module._bound_document_links(source_config, links)
    selected_urls = {url for url, _title in selected}
    assert links[0][0] in selected_urls
    assert links[1][0] in selected_urls and links[2][0] in selected_urls
    assert links[3][0] in selected_urls
    assert links[4][0] not in selected_urls
    assert links[5][0] not in selected_urls


def test_sec_guard_clamps_rate_and_reports_lock_contention(tmp_path, monkeypatch):
    monkeypatch.delenv("EDGAR_RATE_LIMIT_PER_SEC", raising=False)
    monkeypatch.delitem(sys.modules, "edgar", raising=False)
    details = ratelimit_module.configure_edgartools_rate_limit(99)
    assert details["limit"] == 5
    assert details["manager_verified"] is True
    assert ratelimit_module.os.environ["EDGAR_RATE_LIMIT_PER_SEC"] == "5"

    lock_path = tmp_path / "shared-sec.lock"
    with FileLock(str(lock_path)):
        with ratelimit_module.sec_acquisition_guard(tmp_path, timeout=0, lock_path=lock_path) as status:
            assert status["acquired"] is False


def test_sec_checkpoint_without_periodic_refreshes_bounded_lookback(source_config, monkeypatch):
    """A stale/narrow checkpoint cannot hide the latest completed 10-Q."""

    routine = FakeFiling(
        "8-K",
        "0000092230-26-000099",
        "2026-08-17",
        "",
        [FakeAttachment("director.htm", "Director appointment", b"routine", sequence="1", document_type="HTML")],
        items="5.02",
    )
    quarterly = FakeFiling(
        "10-Q",
        "0000092230-26-000100",
        "2026-07-31",
        "2026-06-30",
        [FakeAttachment("tfc-20260630.htm", "10-Q", "Q2 2026", sequence="1", document_type="HTML")],
    )
    calls = []

    class FakeCompany:
        not_found = False

        def __init__(self, cik):
            self.cik = int(cik)

        def get_filings(self, **kwargs):
            calls.append(kwargs.get("filing_date"))
            # The overlap-only query sees only the routine latest event; the
            # fallback normal lookback includes the completed quarter.
            return [routine] if len(calls) == 1 else [routine, quarterly]

    fake_edgar = types.ModuleType("edgar")
    fake_edgar.Company = FakeCompany
    monkeypatch.setitem(sys.modules, "edgar", fake_edgar)

    @contextlib.contextmanager
    def fake_guard(*args, **kwargs):
        yield {"acquired": True, "manager_verified": True}

    monkeypatch.setattr(sec_module, "sec_acquisition_guard", fake_guard)
    monkeypatch.setenv("EDGAR_IDENTITY", "configured-for-test")
    result = sec_module.discover_sec(
        source_config,
        source_config["companies"][0],
        bootstrap=False,
        checkpoints={"TFC:sec": "2026-09-01T00:00:00+00:00"},
    )

    assert len(calls) == 2
    assert [document.period for document in result.documents] == ["2026-Q2"]
    assert result.pending == []
