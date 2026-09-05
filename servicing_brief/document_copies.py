"""Deterministic packaging of archived HTML and its original image assets."""
from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath
from collections.abc import Mapping, Sequence
from urllib.parse import unquote, urlparse
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED
from bs4 import BeautifulSoup


def html_bundle(document, data):
    """Return a local ZIP containing unchanged source bytes and relative names.

    No rendering or PDF generation occurs. An incomplete or unsafe bundle is
    rejected rather than delivered as a complete presentation copy.
    """
    metadata = document.get("metadata", {}) if isinstance(document, dict) else document.metadata
    if not isinstance(metadata, Mapping):
        raise ValueError("HTML bundle metadata must be an object")
    path = Path(document["path"] if isinstance(document, dict) else document.path)
    entries = {}
    original_name = metadata.get("document") or path.name
    assets = metadata.get("assets", [])
    if isinstance(assets, (str, bytes, bytearray)) or not isinstance(assets, Sequence):
        raise ValueError("HTML bundle assets must be a list")

    def add_entry(name, content):
        if not isinstance(name, str) or not name:
            raise ValueError("unsafe source asset filename")
        if name in entries:
            raise ValueError("duplicate source asset filename")
        entries[name] = content

    add_entry(original_name, data)
    for asset in assets:
        if not isinstance(asset, Mapping):
            raise ValueError("HTML bundle asset must be an object")
        try:
            content = Path(asset["path"]).read_bytes()
            expected_hash = str(asset["content_hash"])
            document_name = asset["document"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("HTML bundle asset metadata is incomplete") from exc
        if sha256(content).hexdigest().casefold() != expected_hash.casefold():
            raise ValueError("asset integrity mismatch")
        add_entry(document_name, content)
    for name in entries:
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name or len(relative.parts) != 1:
            raise ValueError("unsafe source asset filename")
    for img in BeautifulSoup(data, "html.parser").find_all("img", src=True):
        src = str(img["src"]).strip()
        if src.casefold().startswith("data:"):
            continue
        image_path = PurePosixPath(unquote(urlparse(src).path))
        # Issuer pages commonly use ./image.png even though the bundle stores
        # flat source names.  Normalize only harmless current-directory
        # segments, while keeping traversal and nested paths unavailable.
        if image_path.is_absolute() or ".." in image_path.parts:
            raise ValueError("source image missing from archive bundle")
        image_parts = tuple(part for part in image_path.parts if part not in {"", "."})
        if len(image_parts) != 1:
            raise ValueError("source image missing from archive bundle")
        image_name = image_parts[0]
        if image_name not in entries:
            raise ValueError("source image missing from archive bundle")
    out = BytesIO()
    with ZipFile(out, "w", compression=ZIP_DEFLATED) as archive:
        for name in sorted(entries):
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, entries[name])
    payload = out.getvalue()
    target = path.with_name(path.stem + "_html-with-assets_" + sha256(payload).hexdigest()[:12] + ".zip")
    if not target.exists():
        target.write_bytes(payload)
    return target, payload
