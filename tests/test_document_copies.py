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


def test_responsive_image_candidates_are_checked_as_local_bundle_entries(tmp_path):
    html = (
        b'<picture><source srcset="small.jpg 1x, large.jpg 2x">'
        b'<img src="small.jpg" srcset="small.jpg 1x, large.jpg 2x"></picture>'
    )
    source = tmp_path / "exhibit.htm"
    small = tmp_path / "small.jpg"
    large = tmp_path / "large.jpg"
    source.write_bytes(html)
    small.write_bytes(b"small image")
    large.write_bytes(b"large image")
    assets = [
        {"document": "small.jpg", "path": str(small), "content_hash": sha256(small.read_bytes()).hexdigest()},
        {"document": "large.jpg", "path": str(large), "content_hash": sha256(large.read_bytes()).hexdigest()},
    ]

    _, payload = html_bundle(
        {"path": str(source), "metadata": {"document": "exhibit.htm", "assets": assets}},
        html,
    )

    with ZipFile(BytesIO(payload)) as bundle:
        assert bundle.read("exhibit.htm") == html
        assert bundle.read("small.jpg") == b"small image"
        assert bundle.read("large.jpg") == b"large image"

    missing = dict(assets[0])
    with pytest.raises(ValueError, match="missing"):
        html_bundle(
            {"path": str(source), "metadata": {"document": "exhibit.htm", "assets": [missing]}},
            html,
        )


def test_external_image_reference_is_not_satisfied_by_local_basename(tmp_path):
    html = b'<img src="https://cdn.example.test/slide.jpg">'
    source = tmp_path / "exhibit.htm"
    image = tmp_path / "slide.jpg"
    source.write_bytes(html)
    image.write_bytes(b"local image")
    metadata = {
        "document": "exhibit.htm",
        "assets": [{
            "document": "slide.jpg",
            "path": str(image),
            "content_hash": sha256(image.read_bytes()).hexdigest(),
        }],
    }

    with pytest.raises(ValueError, match="external"):
        html_bundle({"path": str(source), "metadata": metadata}, html)


@pytest.mark.parametrize("base_href", ["https://cdn.example.test/assets/", "assets/"])
def test_nontrivial_base_href_does_not_make_local_basename_complete(tmp_path, base_href):
    html = f'<base href="{base_href}"><img src="slide.jpg">'.encode()
    source = tmp_path / "exhibit.htm"
    image = tmp_path / "slide.jpg"
    source.write_bytes(html)
    image.write_bytes(b"local image")
    metadata = {
        "document": "exhibit.htm",
        "assets": [{
            "document": "slide.jpg",
            "path": str(image),
            "content_hash": sha256(image.read_bytes()).hexdigest(),
        }],
    }

    with pytest.raises(ValueError, match="base"):
        html_bundle({"path": str(source), "metadata": metadata}, html)


def test_srcset_embedded_data_uri_does_not_create_phantom_archive_entry(tmp_path):
    html = b'<img srcset="data:image/png;base64,AAAA 1x, slide.jpg 2x">'
    source = tmp_path / "exhibit.htm"
    image = tmp_path / "slide.jpg"
    source.write_bytes(html)
    image.write_bytes(b"slide image")
    metadata = {
        "document": "exhibit.htm",
        "assets": [{
            "document": "slide.jpg",
            "path": str(image),
            "content_hash": sha256(image.read_bytes()).hexdigest(),
        }],
    }

    _, payload = html_bundle({"path": str(source), "metadata": metadata}, html)

    with ZipFile(BytesIO(payload)) as bundle:
        assert bundle.read("exhibit.htm") == html
        assert bundle.read("slide.jpg") == b"slide image"


def test_srcset_data_uri_without_descriptor_still_checks_following_candidate(tmp_path):
    html = b'<img srcset="data:image/png;base64,AAAA, missing-slide.jpg 2x">'
    source = tmp_path / "exhibit.htm"
    source.write_bytes(html)

    with pytest.raises(ValueError, match="missing"):
        html_bundle(
            {"path": str(source), "metadata": {"document": "exhibit.htm", "assets": []}},
            html,
        )


def test_srcset_local_filename_with_comma_is_one_candidate(tmp_path):
    html = b'<img srcset="slide,one.jpg 1x">'
    source = tmp_path / "exhibit.htm"
    image = tmp_path / "slide,one.jpg"
    source.write_bytes(html)
    image.write_bytes(b"comma image")
    metadata = {
        "document": "exhibit.htm",
        "assets": [{
            "document": "slide,one.jpg",
            "path": str(image),
            "content_hash": sha256(image.read_bytes()).hexdigest(),
        }],
    }

    _, payload = html_bundle({"path": str(source), "metadata": metadata}, html)

    with ZipFile(BytesIO(payload)) as bundle:
        assert bundle.read("slide,one.jpg") == b"comma image"


def test_unsupported_srcset_descriptor_is_incomplete(tmp_path):
    html = b'<img srcset="slide.jpg 100vw">'
    source = tmp_path / "exhibit.htm"
    image = tmp_path / "slide.jpg"
    source.write_bytes(html)
    image.write_bytes(b"slide image")
    metadata = {
        "document": "exhibit.htm",
        "assets": [{
            "document": "slide.jpg",
            "path": str(image),
            "content_hash": sha256(image.read_bytes()).hexdigest(),
        }],
    }

    with pytest.raises(ValueError, match="descriptor"):
        html_bundle({"path": str(source), "metadata": metadata}, html)


@pytest.mark.parametrize("reference", [r"slide\\image.jpg", "missing%2Fslide.jpg"])
def test_unsafe_local_image_reference_is_not_claimed_complete(tmp_path, reference):
    html = f'<img src="{reference}">'.encode()
    source = tmp_path / "exhibit.htm"
    image = tmp_path / "slide.jpg"
    source.write_bytes(html)
    image.write_bytes(b"slide image")
    metadata = {
        "document": "exhibit.htm",
        "assets": [{
            "document": "slide.jpg",
            "path": str(image),
            "content_hash": sha256(image.read_bytes()).hexdigest(),
        }],
    }

    with pytest.raises(ValueError, match="missing"):
        html_bundle({"path": str(source), "metadata": metadata}, html)
