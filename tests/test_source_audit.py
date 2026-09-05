"""Focused offline regressions for source identity, recovery, and transport safety."""

from __future__ import annotations

import contextlib
import sys
import types
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import httpx
import pytest

from servicing_brief.models import Document, SourceResult
from servicing_brief.sources import collector as collector_module
from servicing_brief.sources import ir as ir_module
from servicing_brief.sources import sec as sec_module


def _config(tmp_path: Path, **sources: object) -> dict:
    return {
        "_storage": str(tmp_path),
        "sources": {"lookback_days": 120, "overlap_days": 14, "bootstrap_lookback_days": 450, **sources},
        "companies": [
            {
                "ticker": "TFC",
                "name": "Truist Financial Corporation",
                "cik": "0000092230",
                "enabled": True,
                "ir_url": "https://ir.example.test/earnings",
                "ir_pages": ["https://ir.example.test/earnings"],
            },
            {
                "ticker": "PFSI",
                "name": "PennyMac Financial Services, Inc.",
                "cik": "0001745916",
                "enabled": True,
                "ir_url": "https://pfsi.example.test/earnings",
                "ir_pages": ["https://pfsi.example.test/earnings"],
            },
        ],
    }


def _document(tmp_path: Path, *, cik: str, ticker: str) -> Document:
    path = tmp_path / f"{ticker}.pdf"
    payload = b"shared CDN bytes"
    path.write_bytes(payload)
    return Document(
        issuer=ticker,
        cik=cik,
        title="Earnings release",
        kind="release",
        source="ir",
        url="https://cdn.example.test/current-release.pdf",
        published="",
        period="2026-Q2",
        path=str(path),
        content_hash=sha256(payload).hexdigest(),
    )


def test_collector_deduplicates_by_cik_not_shared_cdn_url(tmp_path, monkeypatch):
    docs = {
        "TFC": _document(tmp_path, cik="0000092230", ticker="TFC"),
        "PFSI": _document(tmp_path, cik="0001745916", ticker="PFSI"),
    }

    def sec(_config, company, **_kwargs):
        return SourceResult(documents=[docs[company["ticker"]]], checked=[company["ticker"]])

    monkeypatch.setattr(collector_module, "discover_sec", sec)
    monkeypatch.setattr(collector_module, "discover_ir", lambda *_args, **_kwargs: SourceResult())

    result = collector_module.collect(_config(tmp_path))

    assert {document.cik for document in result.documents} == {"0000092230", "0001745916"}


class _Attachment:
    def __init__(self, document: str, description: str, payload: bytes, *, sequence: str = "1"):
        self.document = document
        self.description = description
        self.document_type = "HTML"
        self.sequence_number = sequence
        self.url = "https://www.sec.gov/Archives/example"
        self.purpose = ""
        self.ixbrl = False
        self._payload = payload

    def download(self):
        return self._payload


class _Filing:
    def __init__(self, form: str, accession: str, filing_date: str, report_date: str, attachment: _Attachment, *, cik: str = ""):
        self.form = form
        self.accession_no = accession
        self.filing_date = filing_date
        self.report_date = report_date
        self.company = "TEST ISSUER"
        self.items = ""
        self.attachments = [attachment]
        self.filing_url = f"https://www.sec.gov/Archives/{accession}"
        self.acceptance_datetime = f"{filing_date}T12:00:00.000Z"
        if cik:
            self.cik = cik

    def text(self):
        return "quarterly earnings results"


def _fake_guard(*_args, **_kwargs):
    @contextlib.contextmanager
    def manager():
        yield {"acquired": True, "manager_verified": True}

    return manager()


def test_future_checkpoint_cannot_hide_current_completed_filing(tmp_path, monkeypatch):
    today = datetime.now(timezone.utc).date()
    future_date = (today + timedelta(days=30)).isoformat()
    current = _Filing(
        "10-Q",
        "current",
        (today - timedelta(days=20)).isoformat(),
        "2026-06-30",
        _Attachment("current.htm", "10-Q", b"Q2 2026"),
    )
    future = _Filing(
        "10-Q",
        "future",
        future_date,
        "2026-09-30",
        _Attachment("future.htm", "10-Q", b"Q3 2026"),
    )
    calls: list[str] = []

    class Company:
        not_found = False

        def __init__(self, _cik):
            pass

        def get_filings(self, **kwargs):
            filing_date = str(kwargs.get("filing_date", ""))
            calls.append(filing_date)
            # A future checkpoint used to query only the future row, which
            # then made the periodic-health check pass before date filtering.
            if filing_date and filing_date[:10] > today.isoformat():
                return [future]
            return [current, future]

    monkeypatch.setitem(sys.modules, "edgar", types.SimpleNamespace(Company=Company))
    monkeypatch.setattr(sec_module, "sec_acquisition_guard", _fake_guard)
    monkeypatch.setenv("EDGAR_IDENTITY", "configured-for-test")

    result = sec_module.discover_sec(
        _config(tmp_path),
        _config(tmp_path)["companies"][0],
        checkpoints={"TFC:sec": (today + timedelta(days=90)).isoformat()},
    )

    assert len(calls) == 1
    assert [document.period for document in result.documents] == ["2026-Q2"]
    assert result.errors == []


def test_sec_rejects_filing_row_with_different_cik(tmp_path, monkeypatch):
    filing = _Filing(
        "10-Q",
        "wrong-cik",
        "2026-08-01",
        "2026-06-30",
        _Attachment("wrong.htm", "10-Q", b"Q2 2026"),
        cik="0000000001",
    )

    class Company:
        not_found = False

        def __init__(self, _cik):
            pass

        def get_filings(self, **_kwargs):
            return [filing]

    monkeypatch.setitem(sys.modules, "edgar", types.SimpleNamespace(Company=Company))
    monkeypatch.setattr(sec_module, "sec_acquisition_guard", _fake_guard)
    monkeypatch.setenv("EDGAR_IDENTITY", "configured-for-test")

    result = sec_module.discover_sec(_config(tmp_path), _config(tmp_path)["companies"][0], bootstrap=True)

    assert result.documents == []
    assert result.errors and result.errors[0]["filing_cik"] == "0000000001"


class _Response:
    def __init__(self, status_code: int, content: bytes = b"", headers: dict[str, str] | None = None, url: str = ""):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {"content-type": "text/html"}
        self.url = url
        self.text = content.decode("utf-8", errors="ignore")


def test_ir_redirect_to_private_target_is_blocked_before_request(tmp_path):
    root = "https://ir.example.test/earnings"
    requested: list[str] = []

    class Session:
        def get(self, url, **_kwargs):
            requested.append(url)
            if url.endswith("/robots.txt"):
                return _Response(404, url=url)
            return _Response(302, headers={"location": "http://127.0.0.1/admin"}, url=url)

    with pytest.raises(ir_module.IRSourceError, match="safe public") as caught:
        ir_module._fetch(_config(tmp_path), Session(), {}, {}, root)

    assert caught.value.blocked is True
    assert "127.0.0.1" not in requested


def test_ir_redirect_transport_retry_does_not_reuse_stale_response(tmp_path):
    root = "https://ir.example.test/earnings"
    cdn = "https://cdn.example.test/current-release.pdf"
    requested: list[tuple[str, dict[str, str]]] = []
    cdn_attempts = 0

    class Session:
        def get(self, url, **kwargs):
            nonlocal cdn_attempts
            requested.append((url, dict(kwargs.get("headers", {}))))
            if url.endswith("/robots.txt"):
                return _Response(404, url=url)
            if url == root:
                return _Response(302, headers={"location": cdn}, url=url)
            cdn_attempts += 1
            if cdn_attempts == 1:
                raise httpx.ReadTimeout("temporary transport failure")
            return _Response(200, b"%PDF-fresh", {"content-type": "application/pdf"}, url=cdn)

    fetched = ir_module._fetch(_config(tmp_path, ir_max_retries=1), Session(), {}, {}, root)

    assert fetched.content == b"%PDF-fresh"
    assert cdn_attempts == 2
    assert [url for url, _headers in requested].count(root) == 2


def test_ir_304_corrupt_cache_forces_unconditional_refresh(tmp_path):
    root = "https://ir.example.test/earnings"
    cache = tmp_path / "archive" / ".cache" / "cached.bin"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"corrupt")
    fresh = b"fresh content"
    manifest = {
        root: {
            "etag": '"v1"',
            "cache_path": str(cache),
            "content_hash": sha256(b"expected old content").hexdigest(),
        }
    }
    page_headers: list[dict[str, str]] = []

    class Session:
        def get(self, url, **kwargs):
            if url.endswith("/robots.txt"):
                return _Response(404, url=url)
            page_headers.append(dict(kwargs.get("headers", {})))
            if len(page_headers) == 1:
                return _Response(304, url=root)
            return _Response(200, fresh, {"content-type": "text/html", "etag": '"v2"'}, url=root)

    fetched = ir_module._fetch(_config(tmp_path, ir_max_retries=1), Session(), {}, manifest, root)

    assert fetched.content == fresh
    assert "If-None-Match" in page_headers[0]
    assert "If-None-Match" not in page_headers[1]
    assert manifest[root]["content_hash"] == sha256(fresh).hexdigest()


def test_ir_304_legacy_cache_without_hash_forces_unconditional_refresh(tmp_path):
    root = "https://ir.example.test/earnings"
    cache = tmp_path / "archive" / ".cache" / "legacy.bin"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"legacy cached bytes")
    fresh = b"fresh legacy replacement"
    manifest = {root: {"etag": '"legacy-v1"', "cache_path": str(cache)}}
    page_headers: list[dict[str, str]] = []

    class Session:
        def get(self, url, **kwargs):
            if url.endswith("/robots.txt"):
                return _Response(404, url=url)
            page_headers.append(dict(kwargs.get("headers", {})))
            if len(page_headers) == 1:
                return _Response(304, url=root)
            return _Response(200, fresh, {"content-type": "text/html"}, url=root)

    fetched = ir_module._fetch(_config(tmp_path, ir_max_retries=1), Session(), {}, manifest, root)

    assert fetched.content == fresh
    assert "If-None-Match" in page_headers[0]
    assert "If-None-Match" not in page_headers[1]
    assert manifest[root]["content_hash"] == sha256(fresh).hexdigest()


@pytest.mark.parametrize("host", ["127.1", "2130706433", "0x7f000001"])
def test_ir_rejects_ambiguous_numeric_host_forms(host):
    allowed, reason = ir_module._safe_http_url(f"https://{host}/earnings")

    assert allowed is False
    assert "numeric" in reason


def test_ir_accepts_public_cdn_hostname():
    assert ir_module._safe_http_url("https://cdn.example.test/current-release.pdf")[0] is True


def test_persisted_ir_pending_url_is_retried_directly(tmp_path, monkeypatch):
    root = "https://ir.example.test/earnings"
    pending_url = "https://cdn.example.test/old-release.pdf"
    requested: list[str] = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, url, **_kwargs):
            requested.append(url)
            if url.endswith("/robots.txt"):
                return _Response(404, url=url)
            if url == root:
                return _Response(200, b"<html><body>Current index</body></html>", {"content-type": "text/html"}, url=root)
            return _Response(200, b"%PDF-pending", {"content-type": "application/pdf"}, url=pending_url)

    monkeypatch.setattr(ir_module.httpx, "Client", Client)
    config = _config(tmp_path)
    config["_pending"] = [{"source_key": "TFC:ir", "url": pending_url, "reason": "previous access block"}]

    result = ir_module.discover_ir(config, config["companies"][0])

    assert result.errors == []
    assert len(result.documents) == 1
    assert result.documents[0].metadata["discovery_url"] == pending_url
    assert pending_url in requested


def test_persisted_ir_pending_url_with_other_cik_is_not_reassigned(tmp_path, monkeypatch):
    root = "https://ir.example.test/earnings"
    pending_url = "https://cdn.example.test/old-release.pdf"
    requested: list[str] = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, url, **_kwargs):
            requested.append(url)
            if url.endswith("/robots.txt"):
                return _Response(404, url=url)
            if url == root:
                return _Response(200, b"<html><body>Current index</body></html>", {"content-type": "text/html"}, url=root)
            return _Response(200, b"%PDF-wrong-company", {"content-type": "application/pdf"}, url=pending_url)

    monkeypatch.setattr(ir_module.httpx, "Client", Client)
    config = _config(tmp_path)
    config["_pending"] = [{
        "source_key": "TFC:ir",
        "cik": "0001745916",
        "url": pending_url,
        "reason": "previous access block",
    }]

    result = ir_module.discover_ir(config, config["companies"][0])

    assert result.errors == []
    assert result.documents == []
    assert pending_url not in requested


def test_archive_bytes_rejects_mismatched_hash(tmp_path):
    with pytest.raises(ValueError, match="content hash"):
        from servicing_brief.sources.common import archive_bytes

        archive_bytes(
            tmp_path,
            ticker="TFC",
            period="2026-Q2",
            kind="release",
            payload=b"original",
            extension=".html",
            content_hash="0" * 64,
        )


def test_ir_content_type_prevents_misleading_pdf_archive_suffix(tmp_path):
    fetched = ir_module._Fetched(
        url="https://ir.example.test/Q2-2026-release.pdf",
        final_url="https://cdn.example.test/current-release.pdf",
        status_code=200,
        content=b"<html><h1>Q2 2026 Earnings Release</h1></html>",
        headers={"content-type": "text/html"},
    )

    document = ir_module._collect_document(
        _config(tmp_path),
        _config(tmp_path)["companies"][0],
        fetched,
        "Q2 2026 Earnings Release",
        "https://ir.example.test/earnings",
    )

    assert Path(document.path).suffix == ".html"
    assert document.metadata["archive_extension"] == ".html"
