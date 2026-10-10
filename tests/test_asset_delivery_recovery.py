"""Real MIME packaging after archived HTML bundles gain a new asset version."""

from __future__ import annotations

from dataclasses import replace
from email import policy
from email.parser import BytesParser
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from zipfile import ZipFile

from servicing_brief import delivery
from servicing_brief.models import Document, SourceResult
from servicing_brief.state import State


def _config() -> dict:
    return {
        "email": {
            "from_address": "sender@example.com",
            "recipient": "reviewer@example.com",
        }
    }


def _report(*, body: str = "Legacy Q2 presentation") -> dict:
    html = (
        "<html><body data-brief-company='TFC' data-brief-cik='0000092230' "
        "data-brief-event='2026-Q2'><p>" + body + "</p></body></html>"
    )
    text = (
        "Company: Truist Financial Corporation (TFC)\n"
        "Earnings period: Q2 2026\n\n"
        + body
    )
    return {
        "subject": "TFC | Q2 2026 earnings brief",
        "text": text,
        "html": html,
        "company_identity": {
            "ticker": "TFC",
            "cik": "0000092230",
            "event": "2026-Q2",
            "kind": "earnings_brief",
        },
        "company_reports": {"TFC": {"html": html, "text": text}},
        "generated_at": "2026-08-02T12:00:00+00:00",
    }


def _asset(path: Path, name: str, content: bytes) -> dict[str, str]:
    path.write_bytes(content)
    return {
        "document": name,
        "path": str(path),
        "content_hash": sha256(content).hexdigest(),
    }


def _sec_deck(
    wrapper_path: Path,
    wrapper: bytes,
    image: dict[str, str],
    *,
    url: str = "https://www.sec.gov/Archives/edgar/data/92230/000009223026000123/ex99-1.htm",
    retrieved: str = "2026-08-01T12:00:00+00:00",
) -> Document:
    wrapper_path.write_bytes(wrapper)
    return Document(
        issuer="Truist Financial Corporation",
        cik="0000092230",
        title="Q2 earnings presentation",
        kind="presentation",
        source="sec",
        url=url,
        published="2026-07-29",
        period="2026-Q2",
        path=str(wrapper_path),
        content_hash=sha256(wrapper).hexdigest(),
        accession="0000092230-26-000123",
        retrieved=retrieved,
        discovered_from="https://www.sec.gov/Archives/edgar/data/92230/000009223026000123/0000092230-26-000123-index.html",
        metadata={
            "ticker": "TFC",
            # SEC's source filename is authoritative even if the local archive
            # has been relocated or was downloaded under another name.
            "document": "ex99-1.htm",
            "assets": [image],
        },
    )


def _parse_zip_attachment(path: Path) -> tuple[bytes, bytes, bytes, str, str]:
    message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    plain = message.get_body(preferencelist=("plain",)).get_content()
    html = message.get_body(preferencelist=("html",)).get_content()
    attachments = [
        part
        for part in message.walk()
        if part.get_content_disposition() == "attachment"
    ]
    assert len(attachments) == 1
    attachment = attachments[0]
    assert attachment.get_content_type() == "application/zip"
    payload = attachment.get_payload(decode=True)
    assert payload is not None
    with ZipFile(BytesIO(payload)) as archive:
        names = archive.namelist()
        assert set(names) == {"ex99-1.htm", "slide.jpg"}
        wrapper = archive.read("ex99-1.htm")
        image = archive.read("slide.jpg")
    return payload, wrapper, image, plain, html


def test_pending_legacy_report_repackages_old_bundle_after_new_asset_version(
    tmp_path: Path,
):
    wrapper = b"<html><body><img src=\"slide.jpg\"></body></html>"
    image_v1 = b"SEC slide image version one"
    image_v2 = b"SEC slide image version two"
    old_wrapper_path = tmp_path / "downloaded-wrapper-v1.htm"
    old_image_path = tmp_path / "downloaded-slide-v1.jpg"
    old_image = _asset(old_image_path, "slide.jpg", image_v1)
    old_document = _sec_deck(old_wrapper_path, wrapper, old_image)
    old_document.id = sha256(
        f"{old_document.cik}|{old_document.url}|{old_document.content_hash}".encode()
    ).hexdigest()[:24]
    report = _report()

    # Capture the real MIME attachment before any relocation or newer asset
    # arrives. This is the byte-level baseline for the pending old report.
    baseline_path = delivery.prepare_messages(
        _config(), report, [old_document], tmp_path / "baseline"
    )[0]
    baseline_zip, baseline_wrapper, baseline_image, baseline_plain, baseline_html = _parse_zip_attachment(baseline_path)
    assert baseline_wrapper == wrapper
    assert baseline_image == image_v1
    assert "Legacy Q2 presentation" in baseline_plain
    assert "Legacy Q2 presentation" in baseline_html

    state_path = tmp_path / "state.sqlite3"
    state = State(state_path)
    # Seed a legacy row using the wrapper-only Document ID. State migration on
    # reopen must preserve this report/document identity and its history.
    state.db.execute(
        "INSERT INTO documents VALUES(?,?,?,?,?,?)",
        (
            old_document.id,
            old_document.cik,
            old_document.period,
            old_document.content_hash,
            json.dumps(old_document.to_dict()),
            old_document.retrieved,
        ),
    )
    state.db.commit()
    state.save_report(
        "pending-old-q2",
        report,
        [old_document],
        tmp_path / "pending-old-q2",
        True,
    )
    state.set_report_status("pending-old-q2", "pending")
    original_report_payload = state.db.execute(
        "SELECT payload FROM briefings WHERE id=?", ("pending-old-q2",)
    ).fetchone()[0]
    state.close()

    # The original wrapper and known image are recovered under different local
    # names. Their SEC metadata.document and asset filename remain unchanged.
    relocated_wrapper_path = tmp_path / "recovered-wrapper-under-new-name.htm"
    relocated_image_path = tmp_path / "recovered-known-slide-path.jpg"
    old_wrapper_path.rename(relocated_wrapper_path)
    old_image_path.rename(relocated_image_path)
    relocated_image = {**old_image, "path": str(relocated_image_path)}
    relocated = replace(
        old_document,
        path=str(relocated_wrapper_path),
        metadata={"ticker": "TFC", "document": "ex99-1.htm", "assets": [relocated_image]},
        retrieved="2026-10-08T12:00:00+00:00",
    )

    state = State(state_path)
    repaired = SourceResult(
        documents=[relocated],
        checkpoints={"TFC:sec": "2026-10-08T12:00:00+00:00"},
        checked=["TFC"],
    )
    state.ingest(repaired)
    assert repaired.documents[0].id == old_document.id
    assert state.documents()[0].path == str(relocated_wrapper_path)
    assert state.documents()[0].metadata["assets"][0]["path"] == str(relocated_image_path)
    assert state.db.execute(
        "SELECT payload FROM briefings WHERE id=?", ("pending-old-q2",)
    ).fetchone()[0] == original_report_payload

    # A later SEC rediscovery has exactly the same wrapper bytes and source URL
    # but a changed image. State must retain the old version for the pending
    # report while admitting a distinct newer bundle version.
    new_image_path = tmp_path / "newer-slide-v2.jpg"
    new_image = _asset(new_image_path, "slide.jpg", image_v2)
    newer = replace(
        relocated,
        path=str(tmp_path / "newer-wrapper-download.htm"),
        metadata={"ticker": "TFC", "document": "ex99-1.htm", "assets": [new_image]},
        retrieved="2026-10-09T12:00:00+00:00",
        id=old_document.id,
    )
    Path(newer.path).write_bytes(wrapper)
    state.ingest(
        SourceResult(
            documents=[newer],
            checkpoints={"TFC:sec": "2026-10-09T12:00:00+00:00"},
            checked=["TFC"],
        )
    )
    assert state.db.execute(
        "SELECT payload FROM briefings WHERE id=?", ("pending-old-q2",)
    ).fetchone()[0] == original_report_payload
    state.close()

    # Reopen through the real SQLite state, then regenerate both versions via
    # production delivery. No fake attachment or packaging helper is used.
    state = State(state_path)
    pending_document = state.report_documents("pending-old-q2")[0]
    documents = state.documents()
    newer_document = next(document for document in documents if document.id != pending_document.id)
    assert pending_document.path == str(relocated_wrapper_path)
    assert pending_document.metadata["document"] == "ex99-1.htm"
    assert pending_document.metadata["assets"][0]["path"] == str(relocated_image_path)
    assert newer_document.metadata["assets"][0]["content_hash"] == new_image["content_hash"]
    assert state.db.execute(
        "SELECT payload FROM briefings WHERE id=?", ("pending-old-q2",)
    ).fetchone()[0] == original_report_payload

    regenerated_path = delivery.prepare_messages(
        _config(), report, [pending_document], tmp_path / "regenerated-old"
    )[0]
    regenerated_zip, regenerated_wrapper, regenerated_image, regenerated_plain, regenerated_html = _parse_zip_attachment(regenerated_path)
    assert regenerated_zip == baseline_zip
    assert regenerated_wrapper == wrapper
    assert regenerated_image == image_v1
    assert "Legacy Q2 presentation" in regenerated_plain
    assert "Legacy Q2 presentation" in regenerated_html

    newer_report = _report(body="Newer Q2 presentation image")
    newer_path = delivery.prepare_messages(
        _config(), newer_report, [newer_document], tmp_path / "newer-version"
    )[0]
    newer_zip, newer_wrapper, newer_image, newer_plain, newer_html = _parse_zip_attachment(newer_path)
    assert newer_zip != baseline_zip
    assert newer_wrapper == wrapper
    assert newer_image == image_v2
    assert "Newer Q2 presentation image" in newer_plain
    assert "Newer Q2 presentation image" in newer_html
    assert newer_image != regenerated_image
    state.close()
