"""Deterministic packaging of archived HTML and its original image assets."""
from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED
from bs4 import BeautifulSoup


def html_bundle(document, data):
    """Return a local ZIP containing unchanged source bytes and relative names.

    No rendering or PDF generation occurs. An incomplete or unsafe bundle is
    rejected rather than delivered as a complete presentation copy.
    """
    metadata = document.get("metadata", {}) if isinstance(document, dict) else document.metadata
    path = Path(document["path"] if isinstance(document, dict) else document.path)
    entries = {}
    original_name = metadata.get("document") or path.name
    for name, content in [(original_name, data)]:
        entries[name] = content
    for asset in metadata.get("assets", []):
        content = Path(asset["path"]).read_bytes()
        if sha256(content).hexdigest() != asset["content_hash"]:
            raise ValueError("asset integrity mismatch")
        entries[asset["document"]] = content
    for name in entries:
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name or len(relative.parts) != 1:
            raise ValueError("unsafe source asset filename")
    for img in BeautifulSoup(data, "html.parser").find_all("img", src=True):
        src = img["src"]
        if src.startswith("data:"):
            continue
        image_name = unquote(urlparse(src).path)
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
