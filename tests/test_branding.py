"""Offline proof and fallback tests for issuer branding."""

from __future__ import annotations

import base64
from hashlib import sha256
import json
from pathlib import Path
import zlib

import pytest

from servicing_brief.branding import brand_view, load_brand_registry, lookup_brand, validate_brand_registry


# A real 1x1 RGBA PNG keeps the registry fixture decodable by PyMuPDF.
_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
_SOURCE = "https://issuer.example/investor/brand-guidelines"


def _write_registry(tmp_path: Path, **overrides: object) -> tuple[Path, Path]:
    asset = tmp_path / "issuer.png"
    asset.write_bytes(_PNG)
    entry = {
        "company_name": "Example Mortgage Holdings",
        "primary_color": "#123456",
        "accent_color": "#ABCDEF",
        "public_logo_url": "https://issuer.example/assets/logo.png",
        "logo_alt": "Example Mortgage Holdings wordmark",
        "local_asset_path": asset.name,
        "asset_sha256": sha256(_PNG).hexdigest(),
        "verification": {
            "method": "Verified issuer asset retained for email",
            "verified_on": "2026-09-05",
            "original_asset_sha256": sha256(_PNG).hexdigest(),
            "raster_asset_sha256": sha256(_PNG).hexdigest(),
        },
        "verified": True,
        "source_urls": [_SOURCE],
        **overrides,
    }
    registry = tmp_path / "company_branding.json"
    registry.write_text(
        json.dumps({
            "version": 1,
            "neutral": {"primary_color": "#52606D", "accent_color": "#9AA6B2"},
            "brands": {"EXM": entry},
        }),
        encoding="utf-8",
    )
    return registry, asset


def _rewrite_asset_proof(registry: Path, asset: Path, data: bytes, *, asset_name: str | None = None) -> None:
    asset.write_bytes(data)
    payload = json.loads(registry.read_text(encoding="utf-8"))
    entry = payload["brands"]["EXM"]
    digest = sha256(data).hexdigest()
    entry["asset_sha256"] = digest
    entry["verification"]["original_asset_sha256"] = digest
    entry["verification"]["raster_asset_sha256"] = digest
    if asset_name is not None:
        entry["local_asset_path"] = asset_name
    registry.write_text(json.dumps(payload), encoding="utf-8")


def _zero_dimension_png() -> bytes:
    data = bytearray(_PNG)
    data[16:20] = (0).to_bytes(4, "big")
    data[29:33] = zlib.crc32(data[12:29]).to_bytes(4, "big")
    return bytes(data)


def test_unknown_company_returns_neutral_logo_free_identity() -> None:
    result = lookup_brand("ZZZ", "Unresearched Servicer")

    assert result["company_name"] == "Unresearched Servicer"
    assert result["ticker"] == "ZZZ"
    assert result["verified"] is False
    assert result["gap_reason"]
    assert result["primary_color"] == "#52606D"
    assert result["accent_color"] == "#9AA6B2"
    assert result["public_logo_url"] is None
    assert result["logo_alt"] is None
    assert result["local_asset_path"] is None


def test_exact_verified_ticker_returns_hashed_brand_asset(tmp_path: Path, monkeypatch) -> None:
    registry, asset = _write_registry(tmp_path)
    monkeypatch.setattr("servicing_brief.branding.DEFAULT_REGISTRY_PATH", registry)

    result = lookup_brand("exm", "Example Mortgage Holdings")

    assert validate_brand_registry(registry) == []
    assert result["verified"] is True
    assert result["primary_color"] == "#123456"
    assert result["accent_color"] == "#ABCDEF"
    assert result["public_logo_url"] == "https://issuer.example/assets/logo.png"
    assert result["logo_alt"] == "Example Mortgage Holdings wordmark"
    assert result["local_asset_path"] == str(asset.resolve())
    assert result["gap_reason"] is None
    assert Path(registry).exists() and asset.exists()


def test_ticker_lookup_keeps_callers_display_name_without_name_matching(tmp_path: Path, monkeypatch) -> None:
    registry, _ = _write_registry(tmp_path)
    monkeypatch.setattr("servicing_brief.branding.DEFAULT_REGISTRY_PATH", registry)

    result = lookup_brand("exm", "Display Name From Filing")

    assert result["verified"] is True
    assert result["company_name"] == "Display Name From Filing"


def test_unverified_record_falls_back_even_when_fields_are_complete(tmp_path: Path, monkeypatch) -> None:
    registry, _ = _write_registry(tmp_path, verified=False)
    monkeypatch.setattr("servicing_brief.branding.DEFAULT_REGISTRY_PATH", registry)

    result = lookup_brand("EXM", "Example Mortgage Holdings")

    assert result["verified"] is False
    assert result["public_logo_url"] is None
    assert any("not verified" in error for error in validate_brand_registry(registry))


def test_malformed_color_url_and_tampered_asset_are_rejected(tmp_path: Path, monkeypatch) -> None:
    registry, asset = _write_registry(
        tmp_path,
        primary_color="#12345",
        public_logo_url="http://issuer.example/assets/logo.png",
    )
    asset.write_bytes(_PNG + b"tampered")
    monkeypatch.setattr("servicing_brief.branding.DEFAULT_REGISTRY_PATH", registry)

    result = lookup_brand("EXM", "Example Mortgage Holdings")
    errors = validate_brand_registry(registry)

    assert result["verified"] is False
    assert result["local_asset_path"] is None
    assert result["public_logo_url"] is None
    assert any("hex" in error for error in errors)
    assert any("HTTPS" in error for error in errors)
    assert any("match" in error for error in errors)


@pytest.mark.parametrize(
    "invalid_asset",
    [
        b"\x89PNG\r\n\x1a\n",  # truncated after the signature
        b"\x89PNG\r\n\x1a\nmalformed",  # signature-only fake content
        _zero_dimension_png(),
        b"\xff\xd8\xff\xe0not-a-png",  # non-PNG content with a .png suffix
    ],
)
def test_matching_hash_invalid_png_falls_back_to_neutral_brand(tmp_path: Path, monkeypatch, invalid_asset: bytes) -> None:
    registry, asset = _write_registry(tmp_path)
    _rewrite_asset_proof(registry, asset, invalid_asset)
    monkeypatch.setattr("servicing_brief.branding.DEFAULT_REGISTRY_PATH", registry)

    result = lookup_brand("EXM", "Example Mortgage Holdings")
    view = brand_view("EXM", "Example Mortgage Holdings")
    errors = validate_brand_registry(registry)

    assert result["verified"] is False
    assert result["public_logo_url"] is None
    assert view["verified"] is False
    assert view["local_asset_path"] is None
    assert any("supported image asset" in error for error in errors)


def test_valid_png_with_non_png_suffix_falls_back_to_neutral_brand(tmp_path: Path, monkeypatch) -> None:
    registry, asset = _write_registry(tmp_path)
    png_with_wrong_suffix = tmp_path / "issuer.jpg"
    _rewrite_asset_proof(registry, png_with_wrong_suffix, _PNG, asset_name=png_with_wrong_suffix.name)
    monkeypatch.setattr("servicing_brief.branding.DEFAULT_REGISTRY_PATH", registry)

    result = lookup_brand("EXM", "Example Mortgage Holdings")
    assert result["verified"] is False
    assert result["public_logo_url"] is None


def test_production_brand_assets_are_decodable_and_verified() -> None:
    tickers = {"TD", "RY", "CM", "BNS", "BMO", "PFSI"}

    assert validate_brand_registry() == []
    for ticker in tickers:
        result = lookup_brand(ticker)
        view = brand_view(ticker, result["company_name"])
        assert result["verified"] is True
        assert view["verified"] is True
        assert view["width"] > 0 and view["height"] > 0
        assert view["logo_src"].startswith("data:image/png;base64,")


@pytest.mark.parametrize("reviewed_on", [None, "", "09/05/2026", "2026-02-30", "20260905"])
def test_unverified_review_date_uses_neutral_identity(tmp_path: Path, monkeypatch, reviewed_on) -> None:
    registry, _ = _write_registry(tmp_path)
    payload = json.loads(registry.read_text(encoding="utf-8"))
    payload["brands"]["EXM"]["verification"]["verified_on"] = reviewed_on
    registry.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr("servicing_brief.branding.DEFAULT_REGISTRY_PATH", registry)

    assert lookup_brand("EXM", "Example Mortgage Holdings")["verified"] is False
    assert any("verified_on" in error for error in validate_brand_registry(registry))
