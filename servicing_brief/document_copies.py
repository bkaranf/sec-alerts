"""Deterministic packaging of archived HTML and its original image assets."""
import re
from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath
from collections.abc import Mapping, Sequence
from urllib.parse import unquote, urlparse
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED
from bs4 import BeautifulSoup


def _srcset_urls(value):
    """Return candidate URLs from a responsive-image ``srcset`` value.

    The HTML we package is preserved byte-for-byte, so this parser is only
    used to verify that every candidate can be found in the local bundle.  A
    source-set data URI is already embedded in the original HTML and does not
    need a ZIP entry.  The normal issuer form (URL followed by an optional
    width or density descriptor) is intentionally kept small and deterministic.
    """

    urls = []
    text = str(value or "")
    cursor = 0
    ascii_space = " \t\n\f\r"
    while cursor < len(text):
        while cursor < len(text) and (text[cursor] in ascii_space or text[cursor] == ","):
            cursor += 1
        if cursor >= len(text):
            break
        start = cursor
        # A URL token runs through commas until ASCII whitespace. This is
        # significant for data URIs and for ordinary local names containing a
        # comma. A trailing comma is the candidate separator and means there
        # are no descriptors to skip for this candidate.
        while cursor < len(text) and text[cursor] not in ascii_space:
            cursor += 1
        url = text[start:cursor]
        if url.endswith(","):
            url = url.rstrip(",")
            if url:
                urls.append(url)
            continue
        if url:
            urls.append(url)
        # Skip and validate the optional width/density descriptor. Unsupported
        # responsive syntax is rejected instead of allowing an unresolved URL
        # to hide in a descriptor-shaped token.
        descriptors = []
        while cursor < len(text) and text[cursor] != ",":
            while cursor < len(text) and text[cursor] in ascii_space:
                cursor += 1
            if cursor >= len(text) or text[cursor] == ",":
                break
            descriptor_start = cursor
            while cursor < len(text) and text[cursor] not in ascii_space and text[cursor] != ",":
                cursor += 1
            descriptors.append(text[descriptor_start:cursor])
        if len(descriptors) > 1:
            raise ValueError("unsupported responsive image descriptor")
        if descriptors:
            descriptor = descriptors[0]
            if not (
                re.fullmatch(r"[1-9][0-9]*w", descriptor)
                or re.fullmatch(r"(?:[1-9][0-9]*(?:\.[0-9]+)?|0\.[0-9]+|\.[0-9]+)x", descriptor)
            ):
                raise ValueError("unsupported responsive image descriptor")
        if cursor < len(text):
            cursor += 1
    return urls


def _image_reference_name(value):
    """Resolve a local image reference to its flat bundle entry name."""

    reference = str(value or "").strip()
    if not reference:
        raise ValueError("source image missing from archive bundle")
    parsed = urlparse(reference)
    if reference.casefold().startswith("data:"):
        return None
    # A local file with the same basename cannot satisfy an external URL.  We
    # have no network step in packaging, and changing the HTML to point at a
    # local substitute would alter original source bytes.
    if parsed.scheme or parsed.netloc:
        raise ValueError("external source image unavailable offline")
    if "\\" in reference or "\\" in unquote(parsed.path):
        raise ValueError("source image missing from archive bundle")
    image_path = PurePosixPath(unquote(parsed.path))
    if image_path.is_absolute() or ".." in image_path.parts:
        raise ValueError("source image missing from archive bundle")
    image_parts = tuple(part for part in image_path.parts if part not in {"", "."})
    if len(image_parts) != 1:
        raise ValueError("source image missing from archive bundle")
    return image_parts[0]


def _required_image_names(data):
    """Return every local ``img``/``picture`` image entry referenced by HTML."""

    soup = BeautifulSoup(data, "html.parser")
    references = []
    for image in soup.find_all("img", src=True):
        references.append(image["src"])
    # ``source`` elements are the responsive alternatives inside ``picture``.
    # Checking all srcset attributes also catches an img srcset outside a
    # picture element while retaining the same bounded local-only contract.
    for element in soup.find_all(srcset=True):
        references.extend(_srcset_urls(element["srcset"]))
    for source in soup.find_all("source", src=True):
        references.append(source["src"])
    if any(not str(reference).strip().casefold().startswith("data:") for reference in references):
        for base in soup.find_all("base", href=True):
            href = str(base["href"] or "").strip()
            # A flat offline bundle can preserve only the document's current
            # directory. Any external or nontrivial base changes where a
            # relative image resolves, and must remain an honest fallback.
            if href and href not in {".", "./"}:
                raise ValueError("nontrivial HTML base unavailable offline")
    names = []
    for reference in references:
        name = _image_reference_name(reference)
        if name is not None:
            names.append(name)
    return names


def html_requires_bundle(data):
    """Return whether HTML needs local image-bundle validation."""

    return bool(_required_image_names(data))


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
    for image_name in _required_image_names(data):
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
