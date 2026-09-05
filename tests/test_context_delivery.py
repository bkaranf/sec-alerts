"""Delivery coverage for an annual context attachment on a current brief."""

from __future__ import annotations

from email import policy
from email.parser import BytesParser
from hashlib import sha256
from pathlib import Path

import pytest

from servicing_brief import delivery
from servicing_brief.models import Document


def _document(tmp_path: Path, *, name: str, kind: str, cik: str = "0001745916") -> Document:
    raw = f"{name} source bytes".encode("utf-8")
    path = tmp_path / f"{name}.html"
    path.write_bytes(raw)
    return Document(
        issuer="PennyMac Financial Services, Inc.",
        cik=cik,
        title=f"PFSI {kind} {name}",
        kind=kind,
        source="sec",
        url=f"https://example.test/{name}",
        published="2026-08-04T12:00:00+00:00",
        period="2026-Q2" if kind != "10-K" else "2025-FY",
        path=str(path),
        content_hash=sha256(raw).hexdigest(),
        metadata={"ticker": "PFSI"},
    )


def _config() -> dict:
    return {"email": {"recipient": "reviewer@example.com", "sender": "sender@example.com"}}


def _report(*, context_documents: list[dict] | None = None) -> dict:
    html = "<body data-brief-company='PFSI' data-brief-cik='0001745916' data-brief-event='2026-Q2'><p>Current-quarter readout</p></body>"
    text = "Company: PennyMac Financial Services, Inc. (PFSI)\nEarnings period: Q2 2026\n\nCurrent-quarter readout"
    return {
        "subject": "PFSI | Q2 2026 servicing brief",
        "text": text,
        "html": html,
        "company_identity": {"ticker": "PFSI", "cik": "0001745916", "event": "2026-Q2", "kind": "earnings_brief"},
        "company_reports": {"PFSI": {"html": html, "text": text}},
        "context_documents": context_documents or [],
    }


def test_prepare_messages_attaches_same_cik_annual_context_alongside_current_source(tmp_path: Path) -> None:
    current = _document(tmp_path, name="q2-release", kind="release")
    annual = _document(tmp_path, name="fy25-10k", kind="10-K")
    report = _report(context_documents=[annual.to_dict()])

    path = delivery.prepare_messages(_config(), report, [current], tmp_path / "preview")[0]
    message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    payloads = [
        part.get_payload(decode=True)
        for part in message.walk()
        if part.get_content_disposition() == "attachment"
    ]

    assert Path(current.path).read_bytes() in payloads
    assert Path(annual.path).read_bytes() in payloads
    assert len(payloads) == 2


@pytest.mark.parametrize(
    ("kind", "cik"),
    [("10-Q", "0001745916"), ("10-K", "0000092230")],
)
def test_prepare_messages_rejects_nonannual_or_wrong_issuer_context(
    tmp_path: Path,
    kind: str,
    cik: str,
) -> None:
    current = _document(tmp_path, name="q2-release", kind="release")
    context = _document(tmp_path, name="context", kind=kind, cik=cik)
    report = _report(context_documents=[context.to_dict()])

    with pytest.raises(ValueError, match="Annual context attachments"):
        delivery.prepare_messages(_config(), report, [current], tmp_path / "preview")
