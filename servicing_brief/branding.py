"""Small, fail-closed registry for verified issuer branding.

The registry is presentation metadata. It never changes the financial meaning
of a brief: chart, improvement and pressure colors remain controlled by the
report renderer. An issuer receives a logo and its primary/accent colors only
when its record is verified and its local asset still matches the recorded
hash.
"""

from __future__ import annotations

import hashlib
import base64
import json
from datetime import date
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import urlparse

import pymupdf


DEFAULT_REGISTRY_PATH = Path(__file__).with_name("company_branding.json")

NEUTRAL_BRAND = {
    "primary_color": "#52606D",
    "accent_color": "#9AA6B2",
}

_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
_SHA256 = re.compile(r"^[0-9A-Fa-f]{64}$")
_GENERIC_ALT = {"logo", "company logo", "corporate logo", "brand logo", "issuer logo"}
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class BrandRegistryError(ValueError):
    """Raised when the registry cannot be read or has invalid structure."""


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _hex(value: Any) -> bool:
    return isinstance(value, str) and bool(_HEX.fullmatch(value))


def _https(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    parsed = urlparse(value)
    return parsed.scheme.casefold() == "https" and bool(parsed.netloc) and not parsed.username and not parsed.password


def _asset_path(entry: Mapping[str, Any], root: Path) -> Path | None:
    value = entry.get("local_asset_path")
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = Path(value.strip())
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        return candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None


def _asset_bytes_valid(path: Path) -> bool:
    if path.suffix.casefold() != ".png":
        return False
    try:
        data = path.read_bytes()
    except OSError:
        return False
    return _png_dimensions(data) is not None


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    """Decode a PNG and return positive dimensions, or fail closed."""

    if not data.startswith(_PNG_SIGNATURE):
        return None
    try:
        pixmap = pymupdf.Pixmap(data)
        width, height = int(pixmap.width), int(pixmap.height)
    except Exception:
        # PyMuPDF exposes format errors through extension-specific exception
        # classes, so this validation boundary must treat every decode error
        # as an invalid registry asset.
        return None
    return (width, height) if width > 0 and height > 0 else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_urls(entry: Mapping[str, Any]) -> list[str]:
    sources = entry.get("source_urls", entry.get("sources", []))
    if isinstance(sources, str):
        return [sources]
    if not isinstance(sources, list):
        return []
    urls: list[str] = []
    for source in sources:
        if isinstance(source, str):
            urls.append(source)
        elif isinstance(source, Mapping):
            for key in ("url", "source_url", "href"):
                if isinstance(source.get(key), str):
                    urls.append(source[key])
                    break
    return urls


def _entry_errors(entry: Any, root: Path) -> list[str]:
    if not isinstance(entry, Mapping):
        return ["record must be an object"]
    errors: list[str] = []
    if not _text(entry.get("company_name")):
        errors.append("company_name is required")
    if entry.get("verified") is not True:
        errors.append("record is not verified")
    if not _hex(entry.get("primary_color")) or not _hex(entry.get("accent_color")):
        errors.append("primary_color and accent_color must be six-digit hex values")
    if not _https(entry.get("public_logo_url")):
        errors.append("public_logo_url must use HTTPS")
    logo = _asset_path(entry, root)
    if logo is None or not logo.is_file() or not _asset_bytes_valid(logo):
        errors.append("local_asset_path must reference a supported image asset")
    expected = entry.get("asset_sha256")
    if not isinstance(expected, str) or not _SHA256.fullmatch(expected):
        errors.append("asset_sha256 must be a 64-character SHA-256 value")
    elif logo is not None and logo.is_file():
        try:
            if _sha256(logo).casefold() != expected.casefold():
                errors.append("asset_sha256 does not match local asset")
        except OSError:
            errors.append("local asset cannot be read")
    proof = entry.get("verification")
    if not isinstance(proof, Mapping) or not _text(proof.get("method")):
        errors.append("verification.method is required as asset proof")
    else:
        reviewed_on = _text(proof.get("verified_on"))
        try:
            if date.fromisoformat(reviewed_on).isoformat() != reviewed_on:
                raise ValueError("noncanonical review date")
        except ValueError:
            errors.append("verification.verified_on must be a valid YYYY-MM-DD review date")
        original_hash = proof.get("original_asset_sha256")
        if not isinstance(original_hash, str) or not _SHA256.fullmatch(original_hash):
            errors.append("verification.original_asset_sha256 must be a SHA-256 value")
        raster_hash = proof.get("raster_asset_sha256")
        if not isinstance(raster_hash, str) or not _SHA256.fullmatch(raster_hash):
            errors.append("verification.raster_asset_sha256 must be a SHA-256 value")
        elif isinstance(expected, str) and raster_hash.casefold() != expected.casefold():
            errors.append("verification.raster_asset_sha256 must match asset_sha256")
    alt = _text(entry.get("logo_alt"))
    if not alt or alt.casefold() in _GENERIC_ALT or len(alt) < 3 or "<" in alt or ">" in alt:
        errors.append("logo_alt must meaningfully identify the issuer")
    if not _source_urls(entry):
        errors.append("source_urls are required as verification proof")
    elif any(not _https(url) for url in _source_urls(entry)):
        errors.append("source_urls must use HTTPS")
    return errors


def _read(path: str | Path) -> tuple[dict[str, Any], Path]:
    registry = Path(path).expanduser().resolve()
    try:
        payload = json.loads(registry.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "brands": {}}, registry
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BrandRegistryError(f"cannot read brand registry {registry}") from exc
    if not isinstance(payload, dict) or payload.get("version", 1) != 1:
        raise BrandRegistryError("brand registry must be a version 1 object")
    if not isinstance(payload.get("brands", {}), dict):
        raise BrandRegistryError("brand registry brands must be an object keyed by ticker")
    return payload, registry


def validate_brand_registry(path: str | Path | None = None) -> list[str]:
    """Return all invalid record or neutral-palette fields in ``path``."""

    path = DEFAULT_REGISTRY_PATH if path is None else path
    payload, registry = _read(path)
    errors: list[str] = []
    for ticker, entry in payload.get("brands", {}).items():
        for error in _entry_errors(entry, registry.parent):
            errors.append(f"{ticker}: {error}")
    neutral = payload.get("neutral", {})
    if neutral and not isinstance(neutral, dict):
        errors.append("neutral must be an object")
    elif isinstance(neutral, dict):
        for key in NEUTRAL_BRAND:
            if key in neutral and not _hex(neutral[key]):
                errors.append(f"neutral: {key} must be six-digit hex")
    return errors


def load_brand_registry(path: str | Path | None = None, *, strict: bool = True) -> dict[str, Any]:
    """Load the fixed registry, optionally dropping invalid records."""

    path = DEFAULT_REGISTRY_PATH if path is None else path
    payload, registry = _read(path)
    errors = validate_brand_registry(path)
    if strict and errors:
        raise BrandRegistryError("; ".join(errors))
    neutral = dict(NEUTRAL_BRAND)
    if isinstance(payload.get("neutral"), dict):
        for key in neutral:
            if _hex(payload["neutral"].get(key)):
                neutral[key] = payload["neutral"][key]
    brands = {
        str(ticker).upper(): dict(entry)
        for ticker, entry in payload.get("brands", {}).items()
        if not _entry_errors(entry, registry.parent)
    }
    return {"version": 1, "neutral": neutral, "brands": brands, "path": registry}


def _fallback(ticker: Any, name: Any, neutral: Mapping[str, Any], reason: str) -> dict[str, Any]:
    return {
        "ticker": _text(ticker).upper(),
        "company_name": _text(name) or _text(ticker).upper() or "Unknown company",
        "primary_color": neutral.get("primary_color", NEUTRAL_BRAND["primary_color"]),
        "accent_color": neutral.get("accent_color", NEUTRAL_BRAND["accent_color"]),
        "public_logo_url": None,
        "logo_alt": None,
        "local_asset_path": None,
        "verified": False,
        "gap_reason": reason,
    }


def lookup_brand(ticker: str | None = None, name: str | None = None) -> dict[str, Any]:
    """Look up a verified ticker record; return neutral styling on any gap."""

    try:
        registry = load_brand_registry(DEFAULT_REGISTRY_PATH, strict=False)
    except (BrandRegistryError, OSError, ValueError) as exc:
        return _fallback(ticker, name, NEUTRAL_BRAND, f"Brand registry unavailable: {exc}")
    entry = registry["brands"].get(_text(ticker).upper())
    if entry is None:
        return _fallback(
            ticker,
            name,
            registry["neutral"],
            "No verified brand metadata is registered for this ticker",
        )
    asset = _asset_path(entry, registry["path"].parent)
    return {
        "ticker": _text(ticker).upper(),
        "company_name": _text(name) or _text(entry["company_name"]),
        "primary_color": entry["primary_color"],
        "accent_color": entry["accent_color"],
        "public_logo_url": entry["public_logo_url"],
        "logo_alt": entry["logo_alt"],
        "local_asset_path": str(asset),
        "verified": True,
        "gap_reason": None,
    }


def brand_view(ticker: str, name: str) -> dict[str, Any]:
    """Self-contained preview artwork, with explicit size for email clients."""
    brand = lookup_brand(ticker, name)
    if brand['verified']:
        try:
            asset_path = Path(brand['local_asset_path'])
            if asset_path.suffix.casefold() != ".png":
                raise ValueError("asset suffix is not .png")
            data = asset_path.read_bytes()
            dimensions = _png_dimensions(data)
            if dimensions is None:
                raise ValueError("asset is not a decodable PNG with positive dimensions")
            width, height = dimensions
        except Exception as exc:
            brand = _fallback(ticker, name, NEUTRAL_BRAND, f"Verified branding asset unavailable: {type(exc).__name__}")
            brand['theme'] = page_theme(brand)
            return brand
        scale = min(140 / width, 36 / height, 1)
        brand.update(logo_src='data:image/png;base64,' + base64.b64encode(data).decode('ascii'),
                     width=round(width * scale), height=round(height * scale))
    brand['theme'] = page_theme(brand)
    return brand


def _mix(color: str, other: str, weight: float) -> str:
    return '#' + ''.join(f'{round(int(color[i:i+2], 16) * weight + int(other[i:i+2], 16) * (1-weight)):02X}' for i in (1, 3, 5))


def contrast_ratio(first: str, second: str) -> float:
    def luminance(color: str) -> float:
        channels = [int(color[i:i+2], 16) / 255 for i in (1, 3, 5)]
        channels = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return sum(c*w for c,w in zip(channels, (0.2126, 0.7152, 0.0722)))
    light, dark = sorted((luminance(first), luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def page_theme(brand: Mapping[str, Any]) -> dict[str, str]:
    """One issuer-derived palette for every reader-facing format.

    Exact corporate colors fill the hero; light surfaces are explicit tints,
    not additional claimed official colors. Financial signals are separate.
    """
    primary = str(brand.get('primary_color', NEUTRAL_BRAND['primary_color']))
    accent = str(brand.get('accent_color', NEUTRAL_BRAND['accent_color']))
    if not _hex(primary) or not _hex(accent):
        raise BrandRegistryError('Page theme requires validated corporate colors')
    hero_text = '#FFFFFF' if contrast_ratio(primary, '#FFFFFF') >= 4.5 else '#000000'
    heading = _mix(primary, '#000000', 0.55)
    return {
        'hero_bg': primary.upper(), 'hero_text': hero_text, 'hero_accent': accent.upper(),
        'page_bg': _mix(primary, '#FFFFFF', 0.10),
        'paper_bg': _mix(primary, '#FFFFFF', 0.015),
        'section_bg': _mix(primary, '#FFFFFF', 0.055),
        'heading_color': heading, 'link_color': heading,
        'rule_color': _mix(primary, '#FFFFFF', 0.22),
    }


def validate_page_theme(html: str, ticker: str) -> None:
    """Reject stale/wrong-issuer page colors at the publication boundary."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')
    expected = page_theme(lookup_brand(ticker))
    surfaces = soup.select('[data-brand-surface]')
    roles = {str(node.get('data-brand-surface')) for node in surfaces}
    if not {'hero', 'page', 'paper'} <= roles:
        raise BrandRegistryError('Company brief requires issuer-themed page, paper and summary surfaces')
    mapping = {'hero': 'hero_bg', 'page': 'page_bg', 'paper': 'paper_bg', 'section': 'section_bg'}
    for node in surfaces:
        role = str(node.get('data-brand-surface'))
        if role not in mapping:
            raise BrandRegistryError(f'Unknown company brand surface: {role}')
        style = dict((key.strip().lower(), value.strip().upper()) for key, value in re.findall(r'([\w-]+)\s*:\s*([^;]+)', str(node.get('style', ''))))
        if style.get('background-color', style.get('background')) != expected[mapping[role]]:
            raise BrandRegistryError(f'{ticker} {role} does not use its verified issuer palette')
        if role == 'hero':
            if style.get('color') != expected['hero_text']:
                raise BrandRegistryError(f'{ticker} summary foreground does not match its contrast-tested palette')
            for node_text in node.select('a, h1, h2, p, sup'):
                text_style = dict((k.strip().lower(), v.strip().upper()) for k,v in re.findall(r'([\w-]+)\s*:\s*([^;]+)', str(node_text.get('style', ''))))
                if 'color' in text_style and text_style['color'] != expected['hero_text']:
                    raise BrandRegistryError(f'{ticker} summary text or citation has a stale color')


__all__ = [
    "BrandRegistryError",
    "DEFAULT_REGISTRY_PATH",
    "NEUTRAL_BRAND",
    "load_brand_registry",
    "lookup_brand",
    "validate_brand_registry",
]
