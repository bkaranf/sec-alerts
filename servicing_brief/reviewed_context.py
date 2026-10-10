"""Reviewed source context bound to an exact archived reporting package.

The catalog predates complete bundle fingerprints, so a proof for a document
with local assets may use either the current ``content_fingerprint`` or a
non-empty list of specifically cited ``asset_hashes``.  The latter keeps an
unchanged cited slide useful when a different slide in the same presentation
has been revised.  Wrapper bytes are always checked before a claim is
selected.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
import re
from typing import Any

from .document_identity import asset_manifest, content_fingerprint, verify_document_assets


_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")


def _field(document: Any, name: str, default: Any = "") -> Any:
    if isinstance(document, Mapping):
        return document.get(name, default)
    return getattr(document, name, default)


def _metadata(document: Any) -> Mapping[str, Any]:
    metadata = _field(document, "metadata", {})
    return metadata if isinstance(metadata, Mapping) else {}


def _proof_fingerprint(proof: Mapping[str, Any]) -> str | None:
    values = [
        proof[name]
        for name in ("content_fingerprint", "fingerprint")
        if name in proof and proof[name] not in (None, "")
    ]
    if len({str(value).strip().lower() for value in values}) > 1:
        raise ValueError("reviewed source has conflicting content fingerprint aliases")
    if not values:
        return None
    value = values[0]
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError("reviewed source has an invalid content fingerprint")
    return value.lower()


def _cited_asset_hashes(proof: Mapping[str, Any]) -> list[str] | None:
    if "asset_hashes" not in proof:
        return None
    values = proof.get("asset_hashes")
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(values, Sequence) or not values:
        return None
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            return None
        result.append(value.lower())
    return list(dict.fromkeys(result))


def _wrapper_intact(document: Any, expected: str) -> bool:
    path_value = _field(document, "path", "")
    try:
        actual = hashlib.sha256(Path(path_value).read_bytes()).hexdigest().lower()
    except (OSError, TypeError, ValueError):
        return False
    return actual == expected.lower()


def _asset_path_intact(asset: Mapping[str, Any], expected: str) -> bool:
    path_value = asset.get("path")
    try:
        actual = hashlib.sha256(Path(path_value).read_bytes()).hexdigest().lower()
    except (OSError, TypeError, ValueError):
        return False
    return actual == expected.lower()


def _asset_proof_valid(document: Any, proof: Mapping[str, Any]) -> bool:
    """Validate the bundle proof for one reviewed source."""

    try:
        manifest = asset_manifest(document)
    except (TypeError, ValueError, KeyError):
        return False
    raw_assets = _metadata(document).get("assets", [])
    if not manifest:
        # A proof cannot cite an asset that the current document does not
        # declare.  A fingerprint is still checked when supplied for a
        # wrapper-only source.
        if "asset_hashes" in proof and proof.get("asset_hashes") not in (None, [], ""):
            return False
        try:
            fingerprint = _proof_fingerprint(proof)
            return fingerprint is None or fingerprint == content_fingerprint(document)
        except (TypeError, ValueError, KeyError):
            return False

    try:
        fingerprint = _proof_fingerprint(proof)
    except (TypeError, ValueError, KeyError):
        return False
    if fingerprint is not None:
        try:
            if fingerprint != content_fingerprint(document):
                return False
            verify_document_assets(document)
        except (OSError, TypeError, ValueError, KeyError):
            return False
        return True

    cited = _cited_asset_hashes(proof)
    if not cited or not isinstance(raw_assets, Sequence) or isinstance(raw_assets, (str, bytes, bytearray)):
        return False
    # Check only cited assets on this compatibility path, so an unchanged
    # reviewed slide remains usable when an unrelated slide is revised.
    for digest in cited:
        matches = [
            asset
            for asset in raw_assets
            if isinstance(asset, Mapping)
            and isinstance(asset.get("content_hash"), str)
            and asset["content_hash"].lower() == digest
        ]
        if not matches or not any(_asset_path_intact(asset, digest) for asset in matches):
            return False
    return True


def _proof_value(proof: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = proof.get(name)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def select_context(documents, *, cik, period, new_ids=None, catalog_path=None):
    """Select reviewed entries whose original bytes and bundle proof survive."""

    catalog_path = catalog_path or Path(__file__).with_name("reviewed_context.json")
    if not Path(catalog_path).exists():
        return []
    try:
        catalog = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    if not isinstance(catalog, Mapping):
        return []

    target_cik = str(cik).lstrip("0") or "0"
    eligible = [
        document
        for document in documents
        if (str(_field(document, "cik", "")).lstrip("0") or "0") == target_cik
    ]
    selected = []
    for entry in catalog.get("entries", []):
        if not isinstance(entry, Mapping):
            continue
        entry_cik = str(entry.get("cik", "")).lstrip("0") or "0"
        if entry_cik != target_cik or entry.get("report_period") != period:
            continue
        raw_sources = entry.get("sources")
        if not isinstance(raw_sources, list) or not raw_sources:
            continue
        refs = []
        valid = True
        for proof in raw_sources:
            if not isinstance(proof, Mapping):
                valid = False
                break
            content_hash = _proof_value(proof, "content_hash", "archive_sha256", "sha256")
            source_period = str(proof.get("period", "") or "")
            source_kind = str(proof.get("kind", "") or "")
            if not _SHA256.fullmatch(content_hash) or not source_period or not source_kind:
                valid = False
                break
            candidates = [
                document
                for document in eligible
                if str(_field(document, "content_hash", "")).lower() == content_hash.lower()
                and str(_field(document, "period", "")) == source_period
                and str(_field(document, "kind", "")) == source_kind
            ]
            chosen = next(
                (
                    document
                    for document in candidates
                    if _wrapper_intact(document, content_hash) and _asset_proof_valid(document, proof)
                ),
                None,
            )
            if chosen is None:
                valid = False
                break
            refs.append({**dict(proof), "document_id": _field(chosen, "id", ""), "source_url": _field(chosen, "url", "")})
        if not valid:
            continue
        if new_ids is not None and not any(ref["document_id"] in new_ids for ref in refs):
            continue
        selected.append({**dict(entry), "sources": refs})
    return selected


__all__ = ["select_context"]
