"""Offline checks for the hash-bound reviewed PFSI context catalog."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path

import pytest

from servicing_brief.models import Document
from servicing_brief.archive_paths import ArchivePathMap
from servicing_brief.reviewed_context import select_context


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "data" / "live-collection-tfc-pfsi-corrected.json"
ARCHIVE_ORIGINAL_ROOT = r"C:\Users\bkara\OneDrive\Documents\ChatGPT\SEC Alerts"


def _pfsi_documents() -> list[Document]:
    """Load the real archived PFSI Q2 package used by the reviewed catalog."""

    if not MANIFEST.exists():
        pytest.skip("source-worker PFSI archive manifest is unavailable")
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    documents = [
        Document(**row)
        for row in payload.get("documents", [])
        if row.get("metadata", {}).get("ticker") == "PFSI"
        and row.get("period") == "2026-Q2"
        and row.get("kind") in {"presentation", "10-Q"}
    ]
    # Historical manifests retain their original paths as evidence. Recover
    # this known project move in memory, only when the archived hashes match.
    mapping = ArchivePathMap(ARCHIVE_ORIGINAL_ROOT, ROOT)
    mapped = [mapping.document(doc) for doc in documents]
    unresolved = [
        resolution
        for _, resolutions in mapped for resolution in resolutions
        if resolution["status"] == "unresolved"
    ]
    if not documents or any(item["reason"] == "mapped_missing" for item in unresolved):
        pytest.skip("source-worker PFSI Q2 archive is unavailable")
    assert not unresolved, "Archived PFSI source integrity could not be verified"
    return [doc for doc, _ in mapped]


def _entry_ids(items: list[dict]) -> set[str]:
    return {str(item["id"]) for item in items}


def test_packaged_context_selects_hash_bound_pfsi_entries_from_real_archive() -> None:
    documents = _pfsi_documents()

    selected = select_context(documents, cik="1745916", period="2026-Q2")

    assert _entry_ids(selected) == {"pfsi-q226-retention", "pfsi-q226-liquidity", "pfsi-q226-advance-expense"}
    # The current package deliberately excludes the release, so the
    # release-bound Cenlar expectation cannot be selected from these inputs.
    assert "pfsi-q226-cenlar-outlook" not in _entry_ids(selected)
    assert any("4.1%" in item["text"] for item in selected)
    assert any("$4.0bn" in item["text"] for item in selected)
    for entry in selected:
        assert entry["cik"] == "0001745916"
        assert entry["report_period"] == "2026-Q2"
        assert entry["sources"]
        for proof in entry["sources"]:
            assert proof["document_id"] in {doc.id for doc in documents}
            assert proof["period"] == "2026-Q2"
            assert proof["source_url"].startswith("https://")
            assert proof["location"]


def test_context_rejects_wrong_issuer_period_and_revised_source_bytes(tmp_path: Path) -> None:
    documents = _pfsi_documents()

    assert select_context(documents, cik="92230", period="2026-Q2") == []
    assert select_context(documents, cik="1745916", period="2026-Q3") == []

    presentation = next(doc for doc in documents if doc.kind == "presentation")
    revised_path = tmp_path / "revised-presentation.htm"
    revised_path.write_bytes(Path(presentation.path).read_bytes() + b"\nrevision")
    revised = replace(presentation, path=str(revised_path))
    revised_documents = [revised if doc.id == presentation.id else doc for doc in documents]
    assert _entry_ids(select_context(revised_documents, cik="1745916", period="2026-Q2")) == {"pfsi-q226-advance-expense"}

    # A changed content hash must also fail closed even if the source bytes are
    # still present: the catalog is bound to the reviewed revision.
    hash_revision = replace(presentation, content_hash="0" * 64)
    hash_revision_documents = [hash_revision if doc.id == presentation.id else doc for doc in documents]
    assert _entry_ids(select_context(hash_revision_documents, cik="1745916", period="2026-Q2")) == {
        "pfsi-q226-advance-expense"
    }


def test_context_rejects_changed_reviewed_asset_and_does_not_inherit_future_or_old_context(tmp_path: Path) -> None:
    documents = _pfsi_documents()
    presentation = next(doc for doc in documents if doc.kind == "presentation")
    metadata = deepcopy(presentation.metadata)
    target = next(asset for asset in metadata["assets"] if asset.get("content_hash", "").startswith("dca7"))
    tampered_asset = tmp_path / "tampered-slide.jpg"
    tampered_asset.write_bytes(Path(target["path"]).read_bytes() + b"tamper")
    target["path"] = str(tampered_asset)
    tampered = replace(presentation, metadata=metadata)
    tampered_documents = [tampered if doc.id == presentation.id else doc for doc in documents]
    # The retention entry cites slide 13 and is rejected; the liquidity entry
    # cites a different, still-intact slide and remains independently useful.
    assert _entry_ids(select_context(tampered_documents, cik="1745916", period="2026-Q2")) == {
        "pfsi-q226-liquidity",
        "pfsi-q226-advance-expense",
    }

    assert select_context(documents, cik="1745916", period="2026-Q3") == []
    assert select_context(documents, cik="1745916", period="2026-Q2", new_ids=set()) == []

    ten_q = next(doc for doc in documents if doc.kind == "10-Q")
    presentation_only = select_context(
        documents,
        cik="1745916",
        period="2026-Q2",
        new_ids={presentation.id},
    )
    assert _entry_ids(presentation_only) == {"pfsi-q226-retention", "pfsi-q226-liquidity"}

    ten_q_only = select_context(
        documents,
        cik="1745916",
        period="2026-Q2",
        new_ids={ten_q.id},
    )
    assert _entry_ids(ten_q_only) == {"pfsi-q226-liquidity", "pfsi-q226-advance-expense"}

    # The two 10-Q-backed entries share the same reviewed revision and must
    # both be invalidated when that source changes; the presentation-only
    # retention entry remains independently usable.
    revised_10_q_path = tmp_path / "revised-10q.htm"
    revised_10_q_path.write_bytes(Path(ten_q.path).read_bytes() + b"\nrevision")
    revised_10_q = replace(ten_q, path=str(revised_10_q_path))
    ten_q_revision_documents = [revised_10_q if doc.id == ten_q.id else doc for doc in documents]
    assert _entry_ids(select_context(ten_q_revision_documents, cik="1745916", period="2026-Q2")) == {
        "pfsi-q226-retention"
    }


def test_context_requires_bundle_fingerprint_or_specific_intact_asset_hash(tmp_path: Path) -> None:
    wrapper = tmp_path / "presentation.html"
    wrapper.write_bytes(b"presentation")
    slide_a = tmp_path / "slide-a.png"
    slide_b = tmp_path / "slide-b.png"
    slide_a.write_bytes(b"slide a")
    slide_b.write_bytes(b"slide b")
    assets = [
        {"document": "slide-a.png", "path": str(slide_a), "content_hash": sha256(slide_a.read_bytes()).hexdigest()},
        {"document": "slide-b.png", "path": str(slide_b), "content_hash": sha256(slide_b.read_bytes()).hexdigest()},
    ]
    document = Document(
        issuer="PennyMac Financial Services",
        cik="0001745916",
        title="Presentation",
        kind="presentation",
        source="ir",
        url="https://issuer.example/presentation",
        published="2026-08-01",
        period="2026-Q2",
        path=str(wrapper),
        content_hash=sha256(wrapper.read_bytes()).hexdigest(),
        metadata={"ticker": "PFSI", "assets": assets},
    )
    from servicing_brief.document_identity import content_fingerprint

    fingerprint_catalog = tmp_path / "fingerprint.json"
    fingerprint_catalog.write_text(json.dumps({"entries": [{
        "id": "exact", "cik": "0001745916", "report_period": "2026-Q2", "text": "exact", "sources": [{
            "content_hash": document.content_hash, "content_fingerprint": content_fingerprint(document),
            "period": "2026-Q2", "kind": "presentation", "location": "slide 1",
        }],
    }]}), encoding="utf-8")
    assert _entry_ids(select_context([document], cik="1745916", period="2026-Q2", catalog_path=fingerprint_catalog)) == {"exact"}

    wrapper_only_catalog = tmp_path / "wrapper-only.json"
    wrapper_only_catalog.write_text(json.dumps({"entries": [{
        "id": "stale", "cik": "0001745916", "report_period": "2026-Q2", "text": "stale", "sources": [{
            "content_hash": document.content_hash, "period": "2026-Q2", "kind": "presentation", "location": "slide 1",
        }],
    }]}), encoding="utf-8")
    assert select_context([document], cik="1745916", period="2026-Q2", catalog_path=wrapper_only_catalog) == []

    cited_catalog = tmp_path / "cited.json"
    cited_catalog.write_text(json.dumps({"entries": [{
        "id": "cited", "cik": "0001745916", "report_period": "2026-Q2", "text": "cited", "sources": [{
            "content_hash": document.content_hash, "period": "2026-Q2", "kind": "presentation", "location": "slide 1",
            "asset_hashes": [assets[0]["content_hash"]],
        }],
    }]}), encoding="utf-8")
    slide_b.write_bytes(b"changed unrelated slide")
    assert _entry_ids(select_context([document], cik="1745916", period="2026-Q2", catalog_path=cited_catalog)) == {"cited"}
    slide_a.write_bytes(b"changed cited slide")
    assert select_context([document], cik="1745916", period="2026-Q2", catalog_path=cited_catalog) == []

    # An explicitly supplied malformed or conflicting fingerprint must not
    # fall back to an otherwise valid claim-level asset hash.
    slide_a.write_bytes(b"slide a")
    invalid = json.loads(cited_catalog.read_text(encoding="utf-8"))
    invalid["entries"][0]["sources"][0]["content_fingerprint"] = "malformed"
    invalid_path = tmp_path / "invalid-fingerprint.json"
    invalid_path.write_text(json.dumps(invalid), encoding="utf-8")
    assert select_context([document], cik="1745916", period="2026-Q2", catalog_path=invalid_path) == []

    conflicting = json.loads(cited_catalog.read_text(encoding="utf-8"))
    conflicting["entries"][0]["sources"][0]["content_fingerprint"] = "0" * 64
    conflicting["entries"][0]["sources"][0]["fingerprint"] = "1" * 64
    conflicting_path = tmp_path / "conflicting-fingerprint.json"
    conflicting_path.write_text(json.dumps(conflicting), encoding="utf-8")
    assert select_context([document], cik="1745916", period="2026-Q2", catalog_path=conflicting_path) == []
