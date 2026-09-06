"""Offline checks for hash-bound original analysis catalogs."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

import servicing_brief.analysis as analysis_module
from servicing_brief.analysis import (
    AnalysisValidationError,
    build_analysis_prompt,
    select_analysis,
    validate_analysis,
)
from servicing_brief.evidence import Evidence
from servicing_brief.models import Document


IDENTITY = {
    "ticker": "PFSI",
    "cik": "0001745916",
    "event": "2026-Q2",
    "kind": "earnings_brief",
}


def _document(tmp_path: Path, *, period: str = "2026-Q2", name: str = "presentation", content: bytes = b"source") -> Document:
    path = tmp_path / f"{name}.html"
    path.write_bytes(content)
    return Document(
        issuer="PennyMac Financial Services",
        cik="0001745916",
        title="Second quarter 2026 materials",
        kind="presentation" if name == "presentation" else "10-Q",
        source="ir",
        url=f"https://issuer.example/{name}",
        published="2026-08-01T12:00:00+00:00",
        period=period,
        path=str(path),
        content_hash=sha256(content).hexdigest(),
        metadata={"ticker": "PFSI"},
    )


def _source_proof(document: Document, *, number: int = 1, location: str = "Presentation, slide 16") -> dict:
    return {
        "number": number,
        "source_url": document.url,
        "archive_path": document.path,
        "archive_sha256": document.content_hash,
        "location": location,
        "document_id": document.id,
        "period": document.period,
        "kind": document.kind,
    }


def _catalog(document: Document, *, text: str = "The servicing portfolio grew as onboarding expanded fee-based scale.") -> dict:
    return {
        "version": 1,
        "ticker": "PFSI",
        "cik": "0001745916",
        "report_period": "2026-Q2",
        "title": "AI Analysis",
        "author": "AI",
        "sections": [
            {
                "id": "portfolio-scale",
                "title": "Scale and economics",
                "text": text,
                "sources": [_source_proof(document)],
                "support": [
                    {
                        "kind": "analytical_inference",
                        "claim": "Onboarding can expand fee-based scale.",
                        "source_numbers": [1],
                        "basis": "The presentation describes the onboarding strategy and fee-based economics.",
                        "qualification": "This does not quantify realized savings.",
                    }
                ],
            }
        ],
    }


def _fact(document: Document) -> Evidence:
    return Evidence(
        id="PFSI-servicing-1",
        document_id=document.id,
        issuer=document.issuer,
        ticker="PFSI",
        metric="servicing_fee_income",
        value="12.30",
        raw_value="$12.30 million",
        unit="USD_millions",
        currency="USD",
        period=document.period,
        scope="servicing",
        definition="servicing fee income",
        location="Presentation, slide 16",
        excerpt="Servicing fee income was $12.30 million.",
        source_url=document.url,
        source_kind=document.source,
        source_title=document.title,
        document_kind=document.kind,
        published=document.published,
    )


def test_valid_analysis_accepts_original_long_paragraph_and_normalizes_sources(tmp_path: Path) -> None:
    document = _document(tmp_path)
    long_text = "The servicing portfolio grew as onboarding expanded fee-based scale. " + ("The operating model matters to cash returns. " * 80)
    selected = select_analysis(_catalog(document, text=long_text), [document], identity=IDENTITY, base_dir=tmp_path)

    assert selected is not None
    assert selected["ticker"] == "PFSI"
    assert selected["cik"] == "0001745916"
    assert selected["report_period"] == "2026-Q2"
    assert selected["sections"][0]["text"] == long_text.strip()
    assert selected["sections"][0]["sources"][0]["document_id"] == document.id
    assert selected["sections"][0]["sources"][0]["archive_sha256"] == document.content_hash
    assert selected["sections"][0]["support"][0]["source_numbers"] == [1]


def test_duplicate_source_proofs_reuse_archive_hash_cache(tmp_path: Path, monkeypatch) -> None:
    document = _document(tmp_path)
    catalog = _catalog(document)
    catalog["source_manifest"] = [_source_proof(document, location="Manifest source")]
    original_sha256 = analysis_module.hashlib.sha256
    hash_calls = []

    def counted_sha256(data=b"", *args, **kwargs):
        hash_calls.append(data)
        return original_sha256(data, *args, **kwargs)

    monkeypatch.setattr(analysis_module.hashlib, "sha256", counted_sha256)

    selected = select_analysis(catalog, [document], identity=IDENTITY, base_dir=tmp_path)

    assert selected is not None
    assert len(hash_calls) == 1


def test_duplicate_source_numbers_keep_fail_closed_errors(tmp_path: Path) -> None:
    document = _document(tmp_path)
    catalog = _catalog(document)
    catalog["source_manifest"] = [
        _source_proof(document, number=1, location="First manifest source"),
        _source_proof(document, number=1, location="Duplicate manifest source"),
    ]
    with pytest.raises(AnalysisValidationError, match="duplicated in source_manifest"):
        validate_analysis(catalog, [document], identity=IDENTITY, base_dir=tmp_path)

    catalog = _catalog(document)
    catalog["sections"][0]["sources"] = [
        _source_proof(document, number=1, location="First section source"),
        _source_proof(document, number=1, location="Duplicate section source"),
    ]
    with pytest.raises(AnalysisValidationError, match="duplicated in this section"):
        validate_analysis(catalog, [document], identity=IDENTITY, base_dir=tmp_path)


def test_analysis_archive_change_between_calls_is_detected(tmp_path: Path) -> None:
    document = _document(tmp_path)
    catalog = _catalog(document)

    assert validate_analysis(catalog, [document], identity=IDENTITY, base_dir=tmp_path)
    Path(document.path).write_bytes(b"revised source")

    with pytest.raises(AnalysisValidationError, match="archive_sha256"):
        validate_analysis(catalog, [document], identity=IDENTITY, base_dir=tmp_path)


def test_analysis_candidate_path_change_is_detected_when_archive_path_differs(tmp_path: Path) -> None:
    document = _document(tmp_path)
    archive_path = tmp_path / "archived-copy.html"
    archive_path.write_bytes(b"source")
    catalog = _catalog(document)
    proof = _source_proof(document, location="Archived copy")
    proof["archive_path"] = str(archive_path)
    catalog["sections"][0]["sources"] = [proof]

    assert validate_analysis(catalog, [document], identity=IDENTITY, base_dir=tmp_path)
    Path(document.path).write_bytes(b"revised candidate")

    with pytest.raises(AnalysisValidationError, match="supplied archived document"):
        validate_analysis(catalog, [document], identity=IDENTITY, base_dir=tmp_path)


def test_wrong_company_or_event_is_empty_but_matching_stale_archive_fails_closed(tmp_path: Path) -> None:
    document = _document(tmp_path)
    catalog = _catalog(document)

    assert select_analysis(catalog, [document], identity={**IDENTITY, "ticker": "TFC"}, base_dir=tmp_path) is None
    assert select_analysis(catalog, [document], identity={**IDENTITY, "event": "2026-Q3"}, base_dir=tmp_path) is None

    revised_path = tmp_path / "revised.html"
    revised_path.write_bytes(b"revised source")
    revised = Document(**{**document.to_dict(), "path": str(revised_path)})
    with pytest.raises(AnalysisValidationError, match="supplied archived document"):
        select_analysis(catalog, [revised], identity=IDENTITY, base_dir=tmp_path)


def test_required_document_freshness_rejects_old_essay_for_supporting_update(tmp_path: Path) -> None:
    document = _document(tmp_path)
    new_document = _document(tmp_path, name="release", content=b"new current release")
    catalog = _catalog(document)

    with pytest.raises(AnalysisValidationError, match="required document"):
        select_analysis(
            catalog,
            [document, new_document],
            identity=IDENTITY,
            base_dir=tmp_path,
            required_document_ids={new_document.id},
        )


def test_verified_uncited_fresh_source_manifest_satisfies_required_document(tmp_path: Path) -> None:
    document = _document(tmp_path)
    new_document = _document(tmp_path, name="release", content=b"new current release")
    catalog = _catalog(document)
    catalog["source_manifest"] = [_source_proof(new_document, location="Earnings release")]

    selected = select_analysis(
        catalog,
        [document, new_document],
        identity=IDENTITY,
        base_dir=tmp_path,
        required_document_ids={new_document.id},
    )

    assert selected is not None
    assert selected["source_manifest"][0]["document_id"] == new_document.id
    assert selected["sections"][0]["sources"][0]["document_id"] == document.id
    assert len(selected["sections"]) == 1


def test_stale_source_manifest_archive_is_rejected(tmp_path: Path) -> None:
    document = _document(tmp_path)
    new_document = _document(tmp_path, name="release", content=b"new current release")
    catalog = _catalog(document)
    catalog["source_manifest"] = [_source_proof(new_document, location="Earnings release")]
    Path(new_document.path).write_bytes(b"revised current release")

    with pytest.raises(AnalysisValidationError, match="archive_sha256"):
        select_analysis(
            catalog,
            [document, new_document],
            identity=IDENTITY,
            base_dir=tmp_path,
            required_document_ids={new_document.id},
        )


def test_any_invalid_section_rejects_entire_essay(tmp_path: Path) -> None:
    document = _document(tmp_path)
    catalog = _catalog(document)
    catalog["sections"].append(
        {
            "id": "unbound",
            "text": "This paragraph has no source proof.",
            "sources": [],
        }
    )

    with pytest.raises(AnalysisValidationError, match="non-empty list"):
        validate_analysis(catalog, [document], identity=IDENTITY, base_dir=tmp_path)


def test_support_must_reference_same_section_and_known_evidence(tmp_path: Path) -> None:
    document = _document(tmp_path)
    catalog = _catalog(document)
    catalog["sections"][0]["support"][0]["source_numbers"] = [2]
    with pytest.raises(AnalysisValidationError, match="same section"):
        validate_analysis(catalog, [document], identity=IDENTITY, base_dir=tmp_path)

    catalog = _catalog(document)
    catalog["sections"][0]["support"][0]["evidence_ids"] = ["missing"]
    with pytest.raises(AnalysisValidationError, match="unknown evidence"):
        validate_analysis(catalog, [document], identity=IDENTITY, base_dir=tmp_path, evidence=[_fact(document)])


def test_em_dash_is_rejected_from_reader_facing_analysis(tmp_path: Path) -> None:
    document = _document(tmp_path)
    with pytest.raises(AnalysisValidationError, match="em dash"):
        validate_analysis(_catalog(document, text="A finding\u2014with an unsupported punctuation mark."), [document], identity=IDENTITY, base_dir=tmp_path)


def test_prompt_loads_full_guide_and_complete_company_packet(tmp_path: Path) -> None:
    document = _document(tmp_path)
    guide = tmp_path / "PUBLIC_VOICE.md"
    guide.write_text("# Public voice\nAnalysis mode: finding, evidence, implication and limits.\n", encoding="utf-8")
    fact = _fact(document)
    prompt = build_analysis_prompt(
        identity=IDENTITY,
        documents=[document],
        evidence=[fact],
        commentary=[{"id": "comment-1", "ticker": "PFSI", "cik": "0001745916", "text": "Management described onboarding scale.", "period": "2026-Q2"}],
        guide_path=guide,
    )

    assert "Analysis mode: finding, evidence, implication and limits." in prompt
    assert document.id in prompt
    assert document.content_hash in prompt
    assert fact.id in prompt
    assert "The text may contain full original paragraphs" in prompt
    assert "exact source proofs" in prompt
    assert '"source_manifest"' in prompt
    assert "internal provenance" in prompt
    assert len(prompt) > len(guide.read_text(encoding="utf-8"))


def test_prompt_rejects_mixed_company_records(tmp_path: Path) -> None:
    document = _document(tmp_path)
    other = {**document.to_dict(), "cik": "0000000002", "metadata": {"ticker": "TFC"}}
    with pytest.raises(AnalysisValidationError, match="does not match requested company"):
        build_analysis_prompt(identity=IDENTITY, documents=[document, other], guide_path=tmp_path / "PUBLIC_VOICE.md")
