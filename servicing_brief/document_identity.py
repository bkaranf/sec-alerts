"""Canonical identity and integrity helpers for archived document bundles.

``Document.content_hash`` is deliberately the SHA-256 of the document
wrapper bytes.  HTML exhibits can also carry local image or other related
assets, so a wrapper hash alone is not sufficient to identify the archived
bundle.  This module keeps that historical field unchanged and provides a
second, deterministic fingerprint for the complete bundle.

Only the asset filename and its declared SHA-256 participate in the
fingerprint.  Local paths, retrieval timestamps, URLs, sequence numbers and
other transport metadata are intentionally excluded so moving an archive or
rediscovering it does not create a spurious revision.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
from pathlib import Path
import re
from typing import Any


_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_FINGERPRINT_DOMAIN = "servicing_brief.document_fingerprint.v1"


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _metadata(document: Any) -> Mapping[str, Any]:
    metadata = _field(document, "metadata", {})
    if metadata in (None, ""):
        return {}
    if not isinstance(metadata, Mapping):
        raise ValueError("document metadata must be an object")
    return metadata


def _safe_asset_name(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where}.document must be a non-empty filename")
    # Keep the spelling, including case, exactly as supplied.  In particular,
    # do not normalize case or path separators before hashing.
    if value != value.strip() or "\x00" in value:
        raise ValueError(f"{where}.document is not a safe flat filename")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{where}.document is not a safe flat filename")
    if value in {".", ".."} or any(char in value for char in '/\\:*?"<>|'):
        raise ValueError(f"{where}.document is not a safe flat filename")
    if value.endswith((".", " ")):
        raise ValueError(f"{where}.document is not a safe flat filename")
    # These names cannot be represented safely on Windows, the supported
    # archive platform, even when an extension is appended.
    stem = value.split(".", 1)[0].casefold()
    if stem in {"con", "prn", "aux", "nul"} or (
        len(stem) == 4 and stem[:3] in {"com", "lpt"} and stem[3].isdigit()
    ):
        raise ValueError(f"{where}.document is not a safe flat filename")
    return value


def _asset_hash(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{where}.content_hash must be a 64-character SHA-256 value")
    return value.lower()


def _raw_assets(document: Any) -> list[Mapping[str, Any]]:
    metadata = _metadata(document)
    if "assets" not in metadata:
        return []
    assets = metadata.get("assets")
    if assets is None or isinstance(assets, (str, bytes, bytearray)) or not isinstance(assets, Sequence):
        raise ValueError("document metadata.assets must be a list")
    result: list[Mapping[str, Any]] = []
    for index, asset in enumerate(assets):
        where = f"document.metadata.assets[{index}]"
        if not isinstance(asset, Mapping):
            raise ValueError(f"{where} must be an object")
        result.append(asset)
    return result


def asset_manifest(document: Any) -> list[dict[str, str]]:
    """Return a validated, sorted asset manifest.

    Each returned item has exactly ``document`` and lower-case
    ``content_hash`` keys.  The filename remains case-sensitive.  Duplicate
    filenames are rejected even when their hashes happen to match.  This
    keeps the validated metadata safe for downstream packaging, which writes
    one archive entry per filename.  A filename associated with two different
    hashes is likewise a contradictory manifest.
    """

    by_name: dict[str, str] = {}
    for index, asset in enumerate(_raw_assets(document)):
        where = f"document.metadata.assets[{index}]"
        if "document" not in asset or "content_hash" not in asset:
            raise ValueError(f"{where} requires document and content_hash")
        name = _safe_asset_name(asset.get("document"), where)
        digest = _asset_hash(asset.get("content_hash"), where)
        previous = by_name.get(name)
        if previous is not None:
            if previous != digest:
                raise ValueError(f"{where}.document has conflicting content_hash values")
            raise ValueError(f"{where}.document is duplicated")
        by_name[name] = digest
    return [
        {"document": name, "content_hash": by_name[name]}
        for name in sorted(by_name)
    ]


def _wrapper_hash(document: Any) -> str:
    value = _field(document, "content_hash", "")
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError("document.content_hash must be a 64-character SHA-256 value")
    return value.lower()


def content_fingerprint(document: Any) -> str:
    """Return the deterministic identity hash for a document bundle.

    The preimage is domain-tagged and encoded as canonical JSON.  The output
    is a lower-case 64-character SHA-256 value.  The function performs no
    filesystem reads, making it safe to use for identity and deduplication.
    """

    manifest = asset_manifest(document)
    canonical = {
        "domain": _FINGERPRINT_DOMAIN,
        "wrapper_sha256": _wrapper_hash(document),
        "assets": manifest,
    }
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def verify_document_assets(document: Any) -> bool:
    """Verify every declared asset archive byte against its declared hash.

    ``ValueError`` indicates malformed metadata or an integrity mismatch;
    ``OSError`` is allowed to propagate for an unreadable or missing path.
    The wrapper itself is intentionally outside this helper because callers
    already bind it through ``Document.content_hash`` independently.
    """

    # Validate the canonical fields first, then inspect each raw entry so a
    # duplicate pair with a distinct path cannot hide an unreadable copy.
    asset_manifest(document)
    for index, asset in enumerate(_raw_assets(document)):
        where = f"document.metadata.assets[{index}]"
        path_value = asset.get("path")
        if not isinstance(path_value, (str, Path)) or not str(path_value):
            raise ValueError(f"{where}.path is required")
        path = Path(path_value)
        if not path.is_file():
            raise OSError(f"{where}.path does not point to a file")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        expected = _asset_hash(asset.get("content_hash"), where)
        if actual != expected:
            raise ValueError(f"{where}.content_hash does not match archive bytes")
    return True


__all__ = ["asset_manifest", "content_fingerprint", "verify_document_assets"]
