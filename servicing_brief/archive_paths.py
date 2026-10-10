"""Pure, explicit archive-path migration helpers.

Historical State and analysis records can outlive the checkout that created
them.  This module provides the small amount of path repair needed when an
archive is deliberately moved from one absolute root to another.  It never
searches for files, changes bytes, writes metadata, or performs network I/O.

The mapping is intentionally lexical.  A path is eligible only when it is an
exact descendant of ``old_root``.  The relative suffix is joined to the
resolved destination root and the candidate is rejected if an existing
symlink/reparse point would escape that root.  A candidate is useful only
after its bytes match the caller's already-recorded SHA-256.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import hashlib
import os
from pathlib import Path, PureWindowsPath
import re
from typing import Any


_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_CHUNK_SIZE = 1024 * 1024

# Stable reason codes are part of the inspect/apply boundary.  Callers can
# display them without parsing exception text, while the resolver remains
# deliberately non-throwing for ordinary missing/corrupt archive paths.
REASON_ORIGINAL_INTACT = "original_intact"
REASON_INVALID_HASH = "invalid_expected_hash"
REASON_ORIGINAL_MISSING = "original_missing"
REASON_ORIGINAL_NOT_FILE = "original_not_file"
REASON_ORIGINAL_UNREADABLE = "original_unreadable"
REASON_ORIGINAL_HASH_MISMATCH = "original_hash_mismatch"
REASON_OUTSIDE_OLD_ROOT = "outside_old_root"
REASON_PATH_TRAVERSAL = "path_traversal"
REASON_SOURCE_SYMLINK_ESCAPE = "source_symlink_escape"
REASON_DESTINATION_SYMLINK_ESCAPE = "destination_symlink_escape"
REASON_MAPPED_MISSING = "mapped_missing"
REASON_MAPPED_NOT_FILE = "mapped_not_file"
REASON_MAPPED_UNREADABLE = "mapped_unreadable"
REASON_MAPPED_HASH_MISMATCH = "mapped_hash_mismatch"
REASON_MAPPED_VERIFIED = "mapped_verified"
REASON_MALFORMED_RECORD = "malformed_record"
REASON_MISSING_PATH = "missing_path"
REASON_MISSING_HASH = "missing_expected_hash"
REASON_CONFLICTING_PATH_ALIASES = "conflicting_path_aliases"
REASON_CONFLICTING_HASH_ALIASES = "conflicting_hash_aliases"

_PATH_ALIASES = ("archive_path", "source_path", "path")
_HASH_ALIASES = ("archive_sha256", "sha256", "content_hash", "hash")


def _text_path(value: Any) -> str:
    """Return an os.fspath value without accepting arbitrary objects."""

    try:
        value = os.fspath(value)
    except TypeError:
        raise TypeError("archive path must be text or PathLike") from None
    if isinstance(value, bytes):
        return os.fsdecode(value)
    return str(value)


def _has_parent_part(value: str, *, windows: bool) -> bool:
    """Detect a literal ``..`` component before path normalization."""

    # PurePath retains ``..`` components.  Splitting both separators keeps
    # Windows paths usable when a legacy JSON record was emitted on POSIX.
    separators = r"[\\/]" if windows else r"/"
    return any(part == ".." for part in re.split(separators, value))


def _windows_root_parts(path: PureWindowsPath) -> tuple[str, ...]:
    return tuple(part.casefold() for part in path.parts)


def _is_windows_absolute(value: str) -> bool:
    pure = PureWindowsPath(value)
    # ``PureWindowsPath('/tmp')`` is root-relative rather than absolute.  A
    # drive or UNC anchor is required for a Windows path map.
    return bool(pure.drive and pure.root and pure.is_absolute())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_sha(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _path_alias_values(record: Mapping[str, Any]) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    for name in _PATH_ALIASES:
        if name not in record or record.get(name) in (None, ""):
            continue
        value = record.get(name)
        if not isinstance(value, str):
            values.append((name, ""))
        else:
            values.append((name, value))
    return values


def _hash_alias_values(record: Mapping[str, Any]) -> list[tuple[str, Any]]:
    values: list[tuple[str, Any]] = []
    for name in _HASH_ALIASES:
        if name not in record or record.get(name) in (None, ""):
            continue
        values.append((name, record.get(name)))
    return values


def _resolution(
    *,
    status: str,
    original_path: str,
    path: str | None,
    reason: str,
    docid: str = "",
    assetfilename: str = "",
) -> dict[str, Any]:
    """Build one stable resolution record.

    ``docid`` and ``assetfilename`` are intentionally present even when
    empty.  This gives callers a uniform table for main documents and assets,
    including malformed records that need manual review.
    """

    return {
        "status": status,
        "original_path": original_path,
        "path": path,
        "reason": reason,
        "docid": docid,
        "assetfilename": assetfilename,
    }


class ArchivePathMap:
    """Resolve explicitly mapped paths between two absolute archive roots.

    ``old_root`` may be absent because it is the root from a prior checkout.
    ``new_root`` must already exist as a directory.  On POSIX, Windows drive
    and UNC spellings are matched with :class:`PureWindowsPath` and their
    suffix is joined to the native destination root.  This keeps manifests
    created on Windows inspectable from a POSIX recovery environment.
    """

    def __init__(self, old_root: str | os.PathLike[str], new_root: str | os.PathLike[str]):
        old_text = _text_path(old_root)
        new_text = _text_path(new_root)
        self._windows = _is_windows_absolute(old_text)
        self._old_text = old_text
        if self._windows:
            old_pure = PureWindowsPath(old_text)
            if not old_pure.is_absolute() or not old_pure.drive:
                raise ValueError("old_root must be an absolute path")
            if ".." in old_pure.parts:
                raise ValueError("old_root cannot contain parent traversal")
            self._old_pure = old_pure
            # ``old_root`` may be a Windows path while this process is POSIX,
            # so do not feed it through Path.resolve() in that case.
            if os.name == "nt":
                old_native = Path(old_text).expanduser()
                self._old_native = old_native.resolve(strict=False)
            else:
                self._old_native = None
        else:
            old_native = Path(old_text).expanduser()
            if not old_native.is_absolute():
                raise ValueError("old_root must be an absolute path")
            self._old_native = old_native.resolve(strict=False)
            self._old_pure = None

        new_native = Path(new_text).expanduser()
        if not new_native.is_absolute():
            raise ValueError("new_root must be an absolute path")
        try:
            resolved_new = new_native.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError("new_root must resolve to an existing directory") from exc
        if not resolved_new.is_dir():
            raise ValueError("new_root must resolve to an existing directory")
        self.new_root = resolved_new
        # Public roots are useful to inspect callers.  Keep the old spelling
        # for a foreign Windows root and the resolved native Path otherwise.
        self.old_root = old_text if self._windows and os.name != "nt" else self._old_native

    def _candidate_parts(self, raw: str) -> tuple[tuple[str, ...] | None, str]:
        """Return a lexical suffix and a fixed reason when it is unsafe."""

        if self._windows:
            if _has_parent_part(raw, windows=True):
                return None, REASON_PATH_TRAVERSAL
            parsed = PureWindowsPath(raw)
            if not parsed.is_absolute() or not parsed.drive:
                return None, REASON_OUTSIDE_OLD_ROOT
            root_parts = _windows_root_parts(self._old_pure)
            path_parts = _windows_root_parts(parsed)
            if len(path_parts) < len(root_parts) or path_parts[: len(root_parts)] != root_parts:
                return None, REASON_OUTSIDE_OLD_ROOT
            suffix = tuple(parsed.parts[len(self._old_pure.parts) :])
            # A reparse/symlink check is possible only when both paths use the
            # host's native Windows syntax.  Foreign Windows paths are mapped
            # into the native destination and verified there below.
            if self._old_native is not None:
                native_source = Path(raw)
                try:
                    if native_source.resolve(strict=False).relative_to(self._old_native) is None:
                        return None, REASON_SOURCE_SYMLINK_ESCAPE
                except (OSError, RuntimeError, ValueError):
                    return None, REASON_SOURCE_SYMLINK_ESCAPE
            return suffix, ""

        if _has_parent_part(raw, windows=False):
            return None, REASON_PATH_TRAVERSAL
        parsed = Path(raw).expanduser()
        if not parsed.is_absolute():
            return None, REASON_OUTSIDE_OLD_ROOT
        try:
            suffix_path = parsed.relative_to(self._old_native)
        except ValueError:
            return None, REASON_OUTSIDE_OLD_ROOT
        try:
            # resolve(strict=False) follows existing symlinks and junctions,
            # while still allowing the old root itself to be absent.
            if parsed.resolve(strict=False).relative_to(self._old_native) is None:
                return None, REASON_SOURCE_SYMLINK_ESCAPE
        except (OSError, RuntimeError, ValueError):
            return None, REASON_SOURCE_SYMLINK_ESCAPE
        return tuple(suffix_path.parts), ""

    def _candidate_with_reason(self, path: str | os.PathLike[str]) -> tuple[Path | None, str]:
        try:
            raw = _text_path(path)
        except TypeError:
            return None, REASON_OUTSIDE_OLD_ROOT
        suffix, reason = self._candidate_parts(raw)
        if suffix is None:
            return None, reason
        try:
            candidate = self.new_root.joinpath(*suffix)
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(self.new_root)
        except (OSError, RuntimeError, ValueError):
            return None, REASON_DESTINATION_SYMLINK_ESCAPE
        return candidate, ""

    def candidate(self, path: str | os.PathLike[str]) -> Path | None:
        """Return the mapped destination candidate, without reading bytes."""

        candidate, _reason = self._candidate_with_reason(path)
        return candidate

    def resolve(self, path: str | os.PathLike[str], expected_hash: Any) -> dict[str, Any]:
        """Resolve one path and verify bytes against ``expected_hash``.

        An intact original always wins.  A missing or corrupt original can
        move only through the explicit root mapping and only when the mapped
        file's SHA-256 equals the recorded value.  No duplicate search or
        basename matching is performed.
        """

        try:
            original = _text_path(path)
        except TypeError:
            return _resolution(status="unresolved", original_path="", path=None, reason=REASON_OUTSIDE_OLD_ROOT)
        if not _valid_sha(expected_hash):
            return _resolution(status="unresolved", original_path=original, path=None, reason=REASON_INVALID_HASH)
        expected = str(expected_hash).lower()
        original_path = Path(original).expanduser()
        original_reason = REASON_ORIGINAL_MISSING
        try:
            if original_path.is_file():
                try:
                    if _sha256_file(original_path).lower() == expected:
                        return _resolution(status="unchanged", original_path=original, path=original, reason=REASON_ORIGINAL_INTACT)
                    original_reason = REASON_ORIGINAL_HASH_MISMATCH
                except (OSError, ValueError):
                    original_reason = REASON_ORIGINAL_UNREADABLE
            elif original_path.exists():
                original_reason = REASON_ORIGINAL_NOT_FILE
        except (OSError, ValueError):
            original_reason = REASON_ORIGINAL_UNREADABLE

        mapped, candidate_reason = self._candidate_with_reason(original)
        if mapped is None:
            return _resolution(status="unresolved", original_path=original, path=None, reason=candidate_reason or original_reason)
        try:
            if not mapped.exists():
                return _resolution(status="unresolved", original_path=original, path=None, reason=REASON_MAPPED_MISSING)
            if not mapped.is_file():
                return _resolution(status="unresolved", original_path=original, path=None, reason=REASON_MAPPED_NOT_FILE)
            if _sha256_file(mapped).lower() != expected:
                return _resolution(status="unresolved", original_path=original, path=None, reason=REASON_MAPPED_HASH_MISMATCH)
        except (OSError, ValueError):
            return _resolution(status="unresolved", original_path=original, path=None, reason=REASON_MAPPED_UNREADABLE)
        return _resolution(status="mapped", original_path=original, path=str(mapped), reason=REASON_MAPPED_VERIFIED)

    @staticmethod
    def _decorate(
        resolution: dict[str, Any],
        *,
        docid: Any = "",
        assetfilename: Any = "",
        field: str = "",
    ) -> dict[str, Any]:
        # ``field`` remains an internal call-site label while the public
        # resolution schema stays compact and stable.  Callers that need
        # provenance can use the returned docid/assetfilename pair and their
        # own catalog traversal context.
        resolution["docid"] = str(docid or "")
        resolution["assetfilename"] = str(assetfilename or "")
        return resolution

    def document(self, document: Any) -> tuple[Any, list[dict[str, Any]]]:
        """Return a deep-copied document with explicitly mapped paths.

        The helper accepts a ``Document`` instance or a dictionary snapshot.
        Malformed asset records produce unresolved review rows and leave the
        copied record untouched, allowing a caller to quarantine it without
        aborting the rest of a batch.
        """

        copied = deepcopy(document)
        resolutions: list[dict[str, Any]] = []
        docid = getattr(document, "id", "") if not isinstance(document, Mapping) else document.get("id", "")
        if isinstance(document, Mapping):
            path_value = document.get("path", "")
            hash_value = document.get("content_hash", "")
        else:
            path_value = getattr(document, "path", "")
            hash_value = getattr(document, "content_hash", "")
        if path_value in (None, "") or hash_value in (None, ""):
            resolution = _resolution(
                status="unresolved",
                original_path=str(path_value or ""),
                path=None,
                reason=REASON_MISSING_PATH if path_value in (None, "") else REASON_MISSING_HASH,
            )
            resolutions.append(self._decorate(resolution, docid=docid, field="document"))
        else:
            resolution = self.resolve(path_value, hash_value)
            resolutions.append(self._decorate(resolution, docid=docid, field="document"))
            if resolution["status"] == "mapped":
                if isinstance(copied, Mapping):
                    copied["path"] = resolution["path"]
                else:
                    setattr(copied, "path", resolution["path"])

        metadata = copied.get("metadata") if isinstance(copied, Mapping) else getattr(copied, "metadata", None)
        if not isinstance(metadata, Mapping):
            return copied, resolutions
        if "assets" not in metadata:
            return copied, resolutions
        assets = metadata.get("assets")
        if assets is None or assets == "":
            resolutions.append(self._decorate(_resolution(
                status="unresolved", original_path="", path=None, reason=REASON_MALFORMED_RECORD,
            ), docid=docid, field="asset"))
            return copied, resolutions
        if isinstance(assets, (str, bytes, bytearray)) or not isinstance(assets, Sequence):
            resolutions.append(self._decorate(_resolution(
                status="unresolved", original_path="", path=None, reason=REASON_MALFORMED_RECORD,
            ), docid=docid, field="asset"))
            return copied, resolutions
        for index, asset in enumerate(assets):
            asset_name = self._asset_filename(asset)
            if isinstance(copied, Mapping):
                copied_metadata = copied.get("metadata")
                copied_assets = copied_metadata.get("assets") if isinstance(copied_metadata, Mapping) else None
            else:
                copied_metadata = getattr(copied, "metadata", None)
                copied_assets = copied_metadata.get("assets") if isinstance(copied_metadata, Mapping) else None
            copied_asset = copied_assets[index] if isinstance(copied_assets, Sequence) and index < len(copied_assets) else asset
            resolution = self._record_path_resolution(
                copied_asset,
                docid=docid,
                assetfilename=asset_name,
                field=f"asset[{index}]",
                required=True,
            )
            if resolution is not None:
                resolutions.append(resolution)
        return copied, resolutions

    @staticmethod
    def _asset_filename(asset: Any) -> str:
        if not isinstance(asset, Mapping):
            return ""
        for name in ("document", "assetfilename", "filename"):
            value = asset.get(name)
            if isinstance(value, str) and value:
                return value
        paths = _path_alias_values(asset)
        if paths:
            value = paths[0][1].replace("\\", "/")
            return value.rsplit("/", 1)[-1]
        return ""

    def _record_path_resolution(
        self,
        record: Any,
        *,
        docid: Any,
        assetfilename: Any = "",
        field: str,
        required: bool = False,
    ) -> dict[str, Any] | None:
        if not isinstance(record, Mapping):
            return self._decorate(_resolution(status="unresolved", original_path="", path=None, reason=REASON_MALFORMED_RECORD), docid=docid, assetfilename=assetfilename, field=field)
        for name in _PATH_ALIASES:
            if name in record and record.get(name) not in (None, "") and not isinstance(record.get(name), str):
                return self._decorate(_resolution(status="unresolved", original_path="", path=None, reason=REASON_MALFORMED_RECORD), docid=docid, assetfilename=assetfilename, field=field)
        paths = _path_alias_values(record)
        hashes = _hash_alias_values(record)
        if not paths:
            if hashes:
                return self._decorate(_resolution(status="unresolved", original_path="", path=None, reason=REASON_MISSING_PATH), docid=docid, assetfilename=assetfilename, field=field)
            if required:
                return self._decorate(_resolution(status="unresolved", original_path="", path=None, reason=REASON_MISSING_PATH), docid=docid, assetfilename=assetfilename, field=field)
            return None
        if len({value for _name, value in paths}) != 1:
            return self._decorate(_resolution(status="unresolved", original_path=paths[0][1], path=None, reason=REASON_CONFLICTING_PATH_ALIASES), docid=docid, assetfilename=assetfilename, field=field)
        if not hashes:
            return self._decorate(_resolution(status="unresolved", original_path=paths[0][1], path=None, reason=REASON_MISSING_HASH), docid=docid, assetfilename=assetfilename, field=field)
        if len({str(value).strip() for _name, value in hashes}) != 1:
            return self._decorate(_resolution(status="unresolved", original_path=paths[0][1], path=None, reason=REASON_CONFLICTING_HASH_ALIASES), docid=docid, assetfilename=assetfilename, field=field)
        expected = hashes[0][1]
        if not _valid_sha(expected):
            return self._decorate(_resolution(status="unresolved", original_path=paths[0][1], path=None, reason=REASON_INVALID_HASH), docid=docid, assetfilename=assetfilename, field=field)
        result = self.resolve(paths[0][1], expected)
        result = self._decorate(result, docid=docid, assetfilename=assetfilename, field=field)
        if result["status"] == "mapped" and isinstance(record, dict):
            for name, _value in paths:
                record[name] = result["path"]
        return result

    def catalog(self, catalog: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Map archive paths in an analysis catalog without changing proofs."""

        copied = deepcopy(catalog)
        resolutions: list[dict[str, Any]] = []
        if not isinstance(copied, dict):
            return copied, resolutions

        def process_record(record: Any, field: str) -> None:
            docid = ""
            if isinstance(record, Mapping):
                docid = record.get("document_id", record.get("id", ""))
            result = self._record_path_resolution(record, docid=docid, field=field)
            if result is not None:
                resolutions.append(result)
            if not isinstance(record, dict):
                return
            if "supporting_assets" not in record:
                return
            supporting = record.get("supporting_assets")
            if supporting is None or supporting == "":
                resolutions.append(self._decorate(_resolution(status="unresolved", original_path="", path=None, reason=REASON_MALFORMED_RECORD), docid=docid, field=f"{field}.supporting_assets"))
                return
            if isinstance(supporting, (str, bytes, bytearray)) or not isinstance(supporting, Sequence):
                resolutions.append(self._decorate(_resolution(status="unresolved", original_path="", path=None, reason=REASON_MALFORMED_RECORD), docid=docid, field=f"{field}.supporting_assets"))
                return
            for index, asset in enumerate(supporting):
                asset_name = self._asset_filename(asset)
                result = self._record_path_resolution(asset, docid=docid, assetfilename=asset_name, field=f"{field}.supporting_assets[{index}]")
                if result is not None:
                    resolutions.append(result)

        manifest = copied.get("source_manifest")
        if isinstance(manifest, list):
            for index, record in enumerate(manifest):
                process_record(record, f"source_manifest[{index}]")
        sections = copied.get("sections")
        if isinstance(sections, list):
            for section_index, section in enumerate(sections):
                if not isinstance(section, dict) or not isinstance(section.get("sources"), list):
                    continue
                for source_index, record in enumerate(section["sources"]):
                    process_record(record, f"sections[{section_index}].sources[{source_index}]")

        # Some shipped catalogs keep image or other package members at the
        # catalog root rather than under their source proof.  They still use
        # the same explicit path/hash aliases and are repaired in place in the
        # deep copy.  The original catalog and all proof hashes remain intact.
        if "supporting_assets" in copied:
            supporting = copied.get("supporting_assets")
            if supporting is None or supporting == "":
                resolutions.append(self._decorate(_resolution(status="unresolved", original_path="", path=None, reason=REASON_MALFORMED_RECORD), field="supporting_assets"))
            elif isinstance(supporting, (str, bytes, bytearray)) or not isinstance(supporting, Sequence):
                resolutions.append(self._decorate(_resolution(status="unresolved", original_path="", path=None, reason=REASON_MALFORMED_RECORD), field="supporting_assets"))
            else:
                for index, asset in enumerate(supporting):
                    result = self._record_path_resolution(
                        asset,
                        docid=asset.get("document_id", asset.get("id", "")) if isinstance(asset, Mapping) else "",
                        assetfilename=self._asset_filename(asset),
                        field=f"supporting_assets[{index}]",
                    )
                    if result is not None:
                        resolutions.append(result)
        return copied, resolutions


__all__ = [
    "ArchivePathMap",
    "REASON_ORIGINAL_INTACT",
    "REASON_INVALID_HASH",
    "REASON_ORIGINAL_MISSING",
    "REASON_ORIGINAL_NOT_FILE",
    "REASON_ORIGINAL_UNREADABLE",
    "REASON_ORIGINAL_HASH_MISMATCH",
    "REASON_OUTSIDE_OLD_ROOT",
    "REASON_PATH_TRAVERSAL",
    "REASON_SOURCE_SYMLINK_ESCAPE",
    "REASON_DESTINATION_SYMLINK_ESCAPE",
    "REASON_MAPPED_MISSING",
    "REASON_MAPPED_NOT_FILE",
    "REASON_MAPPED_UNREADABLE",
    "REASON_MAPPED_HASH_MISMATCH",
    "REASON_MAPPED_VERIFIED",
    "REASON_MALFORMED_RECORD",
    "REASON_MISSING_PATH",
    "REASON_MISSING_HASH",
    "REASON_CONFLICTING_PATH_ALIASES",
    "REASON_CONFLICTING_HASH_ALIASES",
]
