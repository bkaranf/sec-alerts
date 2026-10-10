"""Offline tests for explicit archive-root path recovery."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest

from servicing_brief.archive_paths import (
    ArchivePathMap,
    REASON_CONFLICTING_HASH_ALIASES,
    REASON_DESTINATION_SYMLINK_ESCAPE,
    REASON_INVALID_HASH,
    REASON_MAPPED_HASH_MISMATCH,
    REASON_MAPPED_MISSING,
    REASON_PATH_TRAVERSAL,
    REASON_OUTSIDE_OLD_ROOT,
)
from servicing_brief.models import Document


def _sha(value: bytes) -> str:
    return sha256(value).hexdigest()


def test_constructor_requires_absolute_roots_and_existing_destination(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="old_root"):
        ArchivePathMap("relative-old", tmp_path)
    with pytest.raises(ValueError, match="new_root"):
        ArchivePathMap(tmp_path / "old", "relative-new")
    with pytest.raises(ValueError, match="existing directory"):
        ArchivePathMap(tmp_path / "old", tmp_path / "missing")

    # The historical root is allowed to be gone, but the destination must be
    # a real directory before a map can be used.
    mapping = ArchivePathMap(tmp_path / "old-no-longer-present", tmp_path)
    assert mapping.new_root == tmp_path.resolve()


def test_candidate_requires_exact_lexical_prefix_and_rejects_traversal(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    new.mkdir()
    mapping = ArchivePathMap(old, new)

    assert mapping.candidate(old / "nested" / "release.html") == new / "nested" / "release.html"
    assert mapping.candidate(tmp_path / "old-copy" / "release.html") is None
    assert mapping.candidate(old / "nested" / ".." / "release.html") is None
    assert mapping.resolve(old / "nested" / ".." / "release.html", _sha(b"x"))["reason"] == REASON_PATH_TRAVERSAL
    assert mapping.resolve(tmp_path / "outside.html", _sha(b"x"))["reason"] == REASON_OUTSIDE_OLD_ROOT


def test_resolve_keeps_intact_original_and_maps_only_matching_bytes(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    original = old / "q2" / "release.html"
    original.parent.mkdir()
    original.write_bytes(b"original bytes")
    expected = _sha(b"original bytes")
    mapping = ArchivePathMap(old, new)

    unchanged = mapping.resolve(original, expected.upper())
    assert unchanged["status"] == "unchanged"
    assert unchanged["path"] == str(original)

    original.unlink()
    mapped = new / "q2" / "release.html"
    mapped.parent.mkdir()
    mapped.write_bytes(b"original bytes")
    result = mapping.resolve(original, expected)
    assert result["status"] == "mapped"
    assert result["path"] == str(mapped)

    mapped.write_bytes(b"different bytes")
    result = mapping.resolve(original, expected)
    assert result["status"] == "unresolved"
    assert result["reason"] == REASON_MAPPED_HASH_MISMATCH

    mapped.unlink()
    result = mapping.resolve(original, expected)
    assert result["status"] == "unresolved"
    assert result["reason"] == REASON_MAPPED_MISSING
    assert mapping.resolve(original, "not-a-sha")["reason"] == REASON_INVALID_HASH


def test_destination_symlink_escape_is_rejected_without_scanning(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    outside = tmp_path / "outside"
    old.mkdir()
    new.mkdir()
    outside.mkdir()
    escaped = new / "nested"
    try:
        escaped.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this host")
    mapping = ArchivePathMap(old, new)
    result = mapping.resolve(old / "nested" / "release.html", _sha(b"bytes"))
    assert result["status"] == "unresolved"
    assert result["reason"] == REASON_DESTINATION_SYMLINK_ESCAPE


def test_resolved_destination_escape_is_rejected_even_without_symlink_support(tmp_path: Path, monkeypatch) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    outside = tmp_path / "outside"
    old.mkdir()
    new.mkdir()
    outside.mkdir()
    mapping = ArchivePathMap(old, new)
    escaped = new / "nested" / "release.html"
    real_resolve = Path.resolve

    def fake_resolve(path: Path, *args, **kwargs):
        if path == escaped:
            return outside / "release.html"
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fake_resolve)
    result = mapping.resolve(old / "nested" / "release.html", _sha(b"bytes"))
    assert result["status"] == "unresolved"
    assert result["reason"] == REASON_DESTINATION_SYMLINK_ESCAPE


def test_windows_manifest_paths_map_on_native_and_foreign_hosts(tmp_path: Path) -> None:
    new = tmp_path / "new"
    new.mkdir()
    destination = new / "archive" / "deck.html"
    destination.parent.mkdir()
    destination.write_bytes(b"deck")
    mapping = ArchivePathMap(r"C:\legacy\archive", new)

    # PureWindowsPath comparison is case-insensitive and accepts either slash
    # spelling even when the process itself is POSIX.
    old_path = r"c:/LEGACY/archive/archive/deck.html"
    assert mapping.candidate(old_path) == destination
    resolved = mapping.resolve(old_path, _sha(b"deck"))
    assert resolved["status"] == "mapped"
    assert resolved["path"] == str(destination)


def test_document_returns_deepcopy_and_resolves_wrapper_and_assets(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    wrapper = new / "deck.html"
    image = new / "slide.png"
    wrapper.write_bytes(b"wrapper")
    image.write_bytes(b"image")
    wrapper_hash = _sha(b"wrapper")
    image_hash = _sha(b"image")
    source = Document(
        issuer="Test",
        cik="0000000001",
        title="Deck",
        kind="presentation",
        source="ir",
        url="https://issuer.example/deck",
        published="",
        period="2026-Q2",
        path=str(old / "deck.html"),
        content_hash=wrapper_hash,
        id="legacy-doc",
        metadata={"assets": [{"document": "slide.png", "path": str(old / "slide.png"), "content_hash": image_hash}]},
    )
    original = deepcopy(source.to_dict())
    mapped, resolutions = ArchivePathMap(old, new).document(source)

    assert isinstance(mapped, Document)
    assert mapped is not source
    assert mapped.path == str(wrapper)
    assert mapped.metadata["assets"][0]["path"] == str(image)
    assert source.to_dict() == original
    assert {(item["assetfilename"], item["status"]) for item in resolutions} == {
        ("", "mapped"),
        ("slide.png", "mapped"),
    }
    assert all(set(item) == {"status", "original_path", "path", "reason", "docid", "assetfilename"} for item in resolutions)


def test_document_reports_malformed_assets_without_mutating_or_throwing(tmp_path: Path) -> None:
    new = tmp_path / "new"
    new.mkdir()
    source = {
        "id": "legacy-doc",
        "path": str(tmp_path / "missing-wrapper.html"),
        "content_hash": _sha(b"wrapper"),
        "metadata": {"assets": [{"document": "slide.png", "path": str(tmp_path / "missing.png"), "content_hash": "bad"}, "not-an-object"]},
    }
    original = deepcopy(source)
    mapped, resolutions = ArchivePathMap(tmp_path / "old", new).document(source)
    assert isinstance(mapped, dict)
    assert mapped == original
    assert any(row["assetfilename"] == "slide.png" and row["reason"] == REASON_INVALID_HASH for row in resolutions)
    assert any(row["assetfilename"] == "" and row["status"] == "unresolved" for row in resolutions)


def test_catalog_maps_manifest_sections_and_supporting_assets_without_rewriting_proofs(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (new / "release.html").write_bytes(b"release")
    (new / "slide.png").write_bytes(b"slide")
    wrapper_hash = _sha(b"release")
    asset_hash = _sha(b"slide")
    catalog = {
        "source_manifest": [{
            "number": 1,
            "document_id": "doc-1",
            "source_url": "https://issuer.example/release",
            "archive_path": str(old / "release.html"),
            "archive_sha256": wrapper_hash,
            "content_fingerprint": "f" * 64,
            "supporting_assets": [{"document": "slide.png", "path": str(old / "slide.png"), "content_hash": asset_hash}],
        }],
        "sections": [{"sources": [{
            "number": 1,
            "document_id": "doc-1",
            "source_url": "https://issuer.example/release",
            "source_path": str(old / "release.html"),
            "sha256": wrapper_hash,
            "fingerprint": "f" * 64,
        }]}],
        "supporting_assets": [{
            "path": str(old / "slide.png"),
            "sha256": asset_hash,
        }],
    }
    original = deepcopy(catalog)
    mapped, resolutions = ArchivePathMap(old, new).catalog(catalog)
    assert mapped["source_manifest"][0]["archive_path"] == str(new / "release.html")
    assert mapped["source_manifest"][0]["supporting_assets"][0]["path"] == str(new / "slide.png")
    assert mapped["sections"][0]["sources"][0]["source_path"] == str(new / "release.html")
    assert mapped["supporting_assets"][0]["path"] == str(new / "slide.png")
    assert mapped["source_manifest"][0]["archive_sha256"] == wrapper_hash
    assert mapped["source_manifest"][0]["content_fingerprint"] == original["source_manifest"][0]["content_fingerprint"]
    assert catalog == original
    assert {row["docid"] for row in resolutions} == {"", "doc-1"}
    assert {row["assetfilename"] for row in resolutions} == {"", "slide.png"}


def test_catalog_does_not_repair_conflicting_aliases(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (new / "release.html").write_bytes(b"release")
    path_conflict = {
        "document_id": "doc-1",
        "archive_path": str(old / "release.html"),
        "source_path": str(old / "different.html"),
        "archive_sha256": _sha(b"release"),
    }
    hash_conflict = {
        "document_id": "doc-2",
        "archive_path": str(old / "release.html"),
        "archive_sha256": _sha(b"release"),
        "sha256": "0" * 64,
    }
    catalog = {"source_manifest": [path_conflict, hash_conflict]}
    mapped, resolutions = ArchivePathMap(old, new).catalog(catalog)
    assert mapped["source_manifest"] == catalog["source_manifest"]
    assert resolutions[0]["reason"] == "conflicting_path_aliases"
    assert resolutions[1]["reason"] == REASON_CONFLICTING_HASH_ALIASES
