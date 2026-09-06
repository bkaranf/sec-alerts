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
    hero_text = max(('#FFFFFF', '#000000'), key=lambda color: contrast_ratio(primary, color))
    surfaces = [_mix(primary, '#FFFFFF', amount) for amount in (0.10, 0.015, 0.055)]
    heading = _mix(primary, '#000000', 0.55)
    # Future registered colors receive the same contrast guarantee, including
    # very pale primaries. The exact primary still fills the summary panel.
    while min(contrast_ratio(heading, bg) for bg in surfaces) < 7:
        heading = _mix(heading, '#000000', 0.90)
    theme = {
        'hero_bg': primary.upper(), 'hero_text': hero_text, 'hero_accent': accent.upper(),
        'page_bg': _mix(primary, '#FFFFFF', 0.10),
        'paper_bg': _mix(primary, '#FFFFFF', 0.015),
        'section_bg': _mix(primary, '#FFFFFF', 0.055),
        'heading_color': heading, 'body_color': heading, 'link_color': heading,
        'secondary_color': '#4C5968',
        'rule_color': _mix(primary, '#FFFFFF', 0.22),
        'positive_bg': '#EAF2ED', 'positive_color': '#205034',
        'negative_bg': '#FAECEB', 'negative_color': '#782A29',
        'prior_bar_color': '#66798A', 'current_bar_color': heading,
        'zero_color': '#66798A', 'logo_bg': '#FFFFFF',
        'dark_page_bg': _mix(primary, '#0D1117', 0.10),
        'dark_paper_bg': _mix(primary, '#17202A', 0.06),
        'dark_section_bg': _mix(primary, '#202A35', 0.08),
        'dark_heading_color': '#E6EAF0', 'dark_body_color': '#E6EAF0',
        'dark_secondary_color': '#B6C2CF',
        'dark_link_color': _mix(primary, '#FFFFFF', 0.25),
        'dark_rule_color': _mix(primary, '#718194', 0.12),
        'dark_positive_bg': '#173126', 'dark_positive_color': '#A4E7BD',
        'dark_negative_bg': '#3D2226', 'dark_negative_color': '#FFB5AF',
        'dark_prior_bar_color': '#889BAC',
        'dark_current_bar_color': _mix(primary, '#FFFFFF', 0.25),
        'dark_zero_color': '#8092A4', 'dark_logo_bg': '#FFFFFF',
        'dark_hero_bg': primary.upper(), 'dark_hero_text': hero_text,
        'dark_hero_accent': accent.upper(),
    }
    validate_theme_contrast(theme)
    return theme


def validate_theme_contrast(theme: Mapping[str, str]) -> None:
    """Fail closed for every generated light and dark semantic palette."""
    for prefix in ('', 'dark_'):
        pairs = [(fg, bg, minimum) for bg in ('page_bg', 'paper_bg', 'section_bg')
                 for fg, minimum in (('body_color', 7), ('heading_color', 7),
                                     ('secondary_color', 4.5), ('link_color', 4.5))]
        pairs += [('positive_color', 'positive_bg', 7), ('negative_color', 'negative_bg', 7),
                  ('prior_bar_color', 'section_bg', 3), ('current_bar_color', 'section_bg', 3),
                  ('zero_color', 'section_bg', 3), ('hero_text', 'hero_bg', 4.5)]
        for fg, bg, minimum in pairs:
            if contrast_ratio(theme[prefix + fg], theme[prefix + bg]) < minimum:
                raise BrandRegistryError(f'{prefix}{fg} on {bg} fails {minimum}:1 contrast')


def apply_page_theme(html: str, theme: Mapping[str, str]) -> str:
    """Bind explicit inline colors to literal dark rules after rendering.

    Inline pairs are the light fallback for email clients that remove styles.
    Attribute values include both colors so separately themed fragments cannot
    overwrite one another. Original logo pixels and financial geometry remain
    unchanged. No CSS variables or runtime JavaScript are required.
    """
    from bs4 import BeautifulSoup
    validate_theme_contrast(theme)
    soup = BeautifulSoup(html, 'html.parser')
    for previous in soup.select('style[data-servicing-theme]'):
        previous.decompose()
    rules: dict[tuple[str, str], str] = {}
    fg_keys = ('heading_color', 'secondary_color', 'positive_color', 'negative_color', 'hero_text')
    bg_keys = ('page_bg', 'paper_bg', 'section_bg', 'positive_bg', 'negative_bg',
               'prior_bar_color', 'current_bar_color', 'hero_bg', 'logo_bg')
    for node in soup.find_all(style=True):
        original_style = str(node['style'])
        declarations = [part.strip() for part in original_style.split(';') if ':' in part]
        updated_style = original_style
        for declaration in declarations:
            prop, value = (part.strip() for part in declaration.split(':', 1))
            match = re.search(r'#[0-9a-fA-F]{6}\b', value)
            if match:
                color = match.group().upper()
                keys = fg_keys if prop == 'color' else bg_keys if prop in ('background', 'background-color') else ('zero_color', 'rule_color', 'hero_accent') if prop.startswith('border') else ()
                key = next((key for key in keys if theme[key].upper() == color), None)
                if prop == 'color' and node.name == 'a' and not node.find_parent(attrs={'data-brand-surface': 'hero'}):
                    key = 'link_color'
                    color = theme[key]
                    value = value[:match.start()] + color + value[match.end():]
                    updated_style = updated_style.replace(declaration, f'{prop}:{value}')
                if key:
                    target = theme['dark_' + key]
                    attr = 'data-sb-fg' if prop == 'color' else 'data-sb-bg' if prop in ('background', 'background-color') else 'data-sb-' + prop
                    token = color[1:] + '_' + target[1:]
                    node[attr] = token
                    css_prop = 'background-color' if prop == 'background' else prop
                    dark_value = value[:match.start()] + target + value[match.end():] if prop.startswith('border') else target
                    rules[(attr, token)] = f'[{attr}="{token}"]{{{css_prop}:{dark_value}!important;}}'
                    if prop in ('background', 'background-color') and node.name in ('table', 'td', 'th', 'body'):
                        node['bgcolor'] = color
        node['style'] = updated_style
    css = ':root{color-scheme:light dark;supported-color-schemes:light dark;}\n@media (prefers-color-scheme: dark){\n' + '\n'.join(rules.values()) + '\n}'
    style = soup.new_tag('style', attrs={'data-servicing-theme': 'contrast-v1'})
    style.string = css
    if soup.head:
        for name in ('color-scheme', 'supported-color-schemes'):
            if not soup.head.find('meta', attrs={'name': name}):
                soup.head.append(soup.new_tag('meta', attrs={'name': name, 'content': 'light dark'}))
        soup.head.append(style)
    else:
        soup.insert(0, style)
    return str(soup)


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
    if soup.select_one('style[data-servicing-theme]'):
        def inherited_color(node, *, background: bool, dark: bool) -> str:
            for ancestor in (node, *node.parents):
                if not getattr(ancestor, 'attrs', None):
                    continue
                properties = dict((key.strip().lower(), value.strip()) for key, value in re.findall(r'([\w-]+)\s*:\s*([^;]+)', str(ancestor.get('style', ''))))
                value = properties.get('background-color', properties.get('background', '')) if background else properties.get('color', '')
                color = re.fullmatch(r'(#[0-9A-Fa-f]{6})(?:\s*!important)?', value)
                if color:
                    token = ancestor.get('data-sb-bg' if background else 'data-sb-fg', '')
                    if dark and re.fullmatch(r'[0-9A-F]{6}_[0-9A-F]{6}', token):
                        return '#' + token.split('_')[1]
                    return color[1]
            return '#FFFFFF' if background else '#000000'
        for node in soup.find_all():
            if node.name in ('style', 'script', 'title') or node.find_parent('head'):
                continue
            if not any(isinstance(child, str) and child.strip() for child in node.children):
                continue
            for dark in (False, True):
                fg = inherited_color(node, background=False, dark=dark)
                bg = inherited_color(node, background=True, dark=dark)
                if contrast_ratio(fg, bg) < 4.5:
                    raise BrandRegistryError(f'{ticker} rendered {"dark" if dark else "light"} text fails 4.5:1 contrast: {node.get_text(" ", strip=True)[:70]}')


__all__ = [
    "BrandRegistryError",
    "DEFAULT_REGISTRY_PATH",
    "NEUTRAL_BRAND",
    "load_brand_registry",
    "lookup_brand",
    "validate_brand_registry",
]
