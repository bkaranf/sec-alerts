from hashlib import sha256
from servicing_brief.models import Document
from servicing_brief.reporting import _event_date


def test_explicit_earnings_release_date_wins_over_server_timestamp(tmp_path):
    path = tmp_path / "filing.htm"
    raw = b"<p>99.1 Earnings Release issued July 17, 2026.</p>"
    path.write_bytes(raw)
    release = Document("Truist", "0000092230", "Earnings release", "release", "ir", "https://issuer.example/release.pdf", "2026-07-16T22:17:16+00:00", "2026-Q2", str(path), sha256(raw).hexdigest())
    filing = Document("Truist", "0000092230", "8-K", "8-K", "sec", "https://sec.example/8k.htm", "2026-07-17", "2026-Q2", str(path), sha256(raw).hexdigest())
    result = _event_date([release, filing])
    assert result["date"] == "2026-07-17"
    assert result["label"] == "Released"
    assert result["document_id"] == filing.id
    assert "issued July 17, 2026" in result["excerpt"]
    assert _event_date([release])["date"] == ""


def test_sec_filing_date_is_not_claimed_as_release_publication(tmp_path):
    path = tmp_path / "release.htm"
    path.write_text("Undated release")
    release = Document("Issuer", "0000000001", "Earnings", "release", "sec", "https://sec.example/release.htm", "2026-07-29", "2026-Q2", str(path), sha256(path.read_bytes()).hexdigest())
    assert _event_date([release])["label"] == "Filed"


def test_multiline_issuer_issued_release_statement_retains_actual_date(tmp_path):
    path = tmp_path / "8k.htm"
    path.write_text('<p>On July\ufffd29, 2026, PennyMac Financial\nServices, Inc. (the Company) issued a press release and a slide presentation.</p>', encoding="utf-8")
    filing = Document("PennyMac", "0001745916", "8-K", "8-K", "sec", "https://sec.example/8k.htm", "2026-07-30", "2026-Q2", str(path), sha256(path.read_bytes()).hexdigest())
    result = _event_date([filing])
    assert result["label"] == "Released"
    assert result["date"] == "2026-07-29"
