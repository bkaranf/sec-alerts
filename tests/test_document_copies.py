from hashlib import sha256
from io import BytesIO
from zipfile import ZipFile
import pytest
from servicing_brief.document_copies import html_bundle


def test_image_deck_bundle_keeps_exact_source_bytes(tmp_path):
    html = b'<html><img src="slide1.jpg"></html>'
    image = b'original slide image fixture'
    parent = tmp_path / "archived-exhibit.htm"
    asset = tmp_path / "archived-image.jpg"
    parent.write_bytes(html)
    asset.write_bytes(image)
    doc = {"path": str(parent), "metadata": {"document": "exhibit99.htm", "assets": [{"document": "slide1.jpg", "path": str(asset), "content_hash": sha256(image).hexdigest()}]}}
    path, data = html_bundle(doc, html)
    assert path.read_bytes() == data
    assert html_bundle(doc, html)[1] == data
    with ZipFile(BytesIO(data)) as bundle:
        assert bundle.read("exhibit99.htm") == html
        assert bundle.read("slide1.jpg") == image
    asset.write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="integrity"):
        html_bundle(doc, html)


def test_incomplete_image_deck_is_not_claimed_as_complete(tmp_path):
    doc = {"path": str(tmp_path / "exhibit.htm"), "metadata": {"document": "exhibit.htm", "assets": []}}
    with pytest.raises(ValueError, match="missing"):
        html_bundle(doc, b'<img src="not-archived.jpg">')
