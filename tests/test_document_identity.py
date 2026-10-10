"""Canonical archive-bundle identity and integrity checks."""

from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from servicing_brief.document_identity import (
    asset_manifest,
    content_fingerprint,
    verify_document_assets,
)
from servicing_brief.models import Document


def _document(tmp_path: Path, *, assets=(), stamp="2026-10-09T00:00:00Z") -> Document:
    wrapper = tmp_path / "deck.html"
    wrapper.write_bytes(b"<html><img src='Slide.PNG'></html>")
    return Document(
        issuer="Test issuer",
        cik="0000000001",
        title="Presentation",
        kind="presentation",
        source="ir",
        url="https://issuer.example/deck",
        published="2026-10-01",
        period="2026-Q3",
        path=str(wrapper),
        content_hash=sha256(wrapper.read_bytes()).hexdigest(),
        retrieved=stamp,
        metadata={"document": "deck.html", "assets": list(assets)},
    )


def _asset(tmp_path: Path, name: str, payload: bytes) -> dict:
    path = tmp_path / (sha256(payload).hexdigest() + ".bin")
    path.write_bytes(payload)
    return {"document": name, "path": str(path), "content_hash": sha256(payload).hexdigest()}


def test_fingerprint_is_order_path_and_time_invariant_but_tracks_bundle_changes(tmp_path: Path) -> None:
    first = _asset(tmp_path, "Slide.PNG", b"slide")
    second = _asset(tmp_path, "notes.txt", b"notes")
    original = _document(tmp_path, assets=[first, second])
    reordered = replace(
        original,
        retrieved="2030-01-01T00:00:00Z",
        metadata={"document": "deck.html", "assets": [second, first]},
    )
    assert content_fingerprint(original) == content_fingerprint(reordered)
    assert asset_manifest(reordered) == [
        {"document": "Slide.PNG", "content_hash": first["content_hash"]},
        {"document": "notes.txt", "content_hash": second["content_hash"]},
    ]

    added = _asset(tmp_path, "extra.txt", b"extra")
    changed_asset = _asset(tmp_path, "notes.txt", b"changed")
    changed = replace(original, metadata={"assets": [first, changed_asset]})
    assert content_fingerprint(replace(original, metadata={"assets": [first, second, added]})) != content_fingerprint(original)
    assert content_fingerprint(changed) != content_fingerprint(original)


@pytest.mark.parametrize(
    "asset",
    [
        {"document": "../slide.png", "content_hash": "0" * 64},
        {"document": "nested/slide.png", "content_hash": "0" * 64},
        {"document": "slide.png", "content_hash": "not-a-sha"},
        {"document": "", "content_hash": "0" * 64},
    ],
)
def test_manifest_rejects_unsafe_names_and_malformed_hashes(tmp_path: Path, asset: dict) -> None:
    with pytest.raises(ValueError):
        asset_manifest(_document(tmp_path, assets=[asset]))


def test_manifest_rejects_conflicting_and_exact_duplicate_names(tmp_path: Path) -> None:
    one = _asset(tmp_path, "slide.png", b"one")
    duplicate = {**one}
    with pytest.raises(ValueError, match="duplicated"):
        asset_manifest(_document(tmp_path, assets=[one, duplicate]))
    conflict = {**one, "content_hash": "0" * 64}
    with pytest.raises(ValueError, match="conflicting"):
        asset_manifest(_document(tmp_path, assets=[one, conflict]))


def test_verify_assets_detects_missing_and_changed_bytes(tmp_path: Path) -> None:
    asset = _asset(tmp_path, "slide.png", b"one")
    document = _document(tmp_path, assets=[asset])
    assert verify_document_assets(document) is True
    Path(asset["path"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="does not match"):
        verify_document_assets(document)
