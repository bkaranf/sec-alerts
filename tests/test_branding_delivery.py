"""Offline checks for fail-closed issuer logo embedding in generated email."""

from __future__ import annotations

from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from hashlib import sha256
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader

from servicing_brief import delivery


_PNG_A = b"\x89PNG\r\n\x1a\nissuer-a-png"
_PNG_B = b"\x89PNG\r\n\x1a\nissuer-b-png"


def _brand(tmp_path: Path, ticker: str, data: bytes, *, verified: bool = True) -> dict[str, object]:
    path = tmp_path / f"{ticker}.png"
    path.write_bytes(data)
    return {
        "ticker": ticker,
        "company_name": f"{ticker} Mortgage Holdings",
        "primary_color": "#123456",
        "accent_color": "#ABCDEF",
        "public_logo_url": f"https://{ticker.lower()}.example/logo.svg",
        "logo_alt": f"{ticker} verified wordmark",
        "local_asset_path": str(path),
        "verified": verified,
        "gap_reason": None if verified else "issuer logo proof is incomplete",
    }


def _message(html_body: str, *, key: str = "k" * 64) -> EmailMessage:
    message = EmailMessage(policy=policy.SMTP)
    message[delivery._KEY_HEADER] = key
    message.set_content("Plain body remains unchanged.")
    message.add_alternative(html_body, subtype="html")
    return message


def _parts(message: EmailMessage, content_type: str) -> list[EmailMessage]:
    return [part for part in message.walk() if part.get_content_type() == content_type]


def test_verified_logo_is_cid_embedded_with_exact_png_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    brand = _brand(tmp_path, "EXM", _PNG_A)
    monkeypatch.setattr(delivery, "lookup_brand", lambda ticker: brand if ticker == "EXM" else None)
    source = brand["public_logo_url"]
    html = f'<p>Keep this copy.</p><img data-brand-ticker="EXM" src="{source}" alt="EXM verified wordmark" width="120" height="32">'
    message = _message(html)

    rewritten = delivery.embed_brand_logos(message, html)
    raw = message.as_bytes(policy=policy.SMTP)
    parsed = BytesParser(policy=policy.default).parsebytes(raw)
    html_part = parsed.get_body(preferencelist=("html",))
    images = _parts(parsed, "image/png")

    assert rewritten in html_part.get_content()
    assert 'src="cid:brand-exm-' in html_part.get_content()
    assert 'alt="EXM verified wordmark" width="120" height="32"' in html_part.get_content()
    assert len(images) == 1
    cid = images[0]["Content-ID"].strip("<>")
    assert f"src=\"cid:{cid}\"" in html_part.get_content()
    assert images[0].get_payload(decode=True) == _PNG_A
    assert sha256(images[0].get_payload(decode=True)).hexdigest() == sha256(_PNG_A).hexdigest()


def test_multiple_issuers_get_distinct_related_parts_and_only_src_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exm = _brand(tmp_path, "EXM", _PNG_A)
    zed = _brand(tmp_path, "ZED", _PNG_B)
    brands = {"EXM": exm, "ZED": zed}
    monkeypatch.setattr(delivery, "lookup_brand", lambda ticker: brands.get(ticker))
    html = (
        '<p>Same body wording.</p>'
        '<img data-brand-ticker=\'EXM\' src="https://exm.example/logo.svg" alt="EXM verified wordmark" width="100">'
        '<img class="issuer-mark" data-brand-ticker="ZED" src=\'data:image/png;base64,\x89PNG\' alt="ZED verified wordmark" height="24">'
    )
    # Use the exact local PNG as the second source, while retaining the first
    # tag's public URL path.
    import base64

    html = html.replace("data:image/png;base64,\x89PNG", "data:image/png;base64," + base64.b64encode(_PNG_B).decode())
    message = _message(html)

    delivery.embed_brand_logos(message, html)
    parsed = BytesParser(policy=policy.default).parsebytes(message.as_bytes(policy=policy.SMTP))
    html_text = parsed.get_body(preferencelist=("html",)).get_content()
    images = _parts(parsed, "image/png")

    assert html_text.count("data-brand-ticker") == 2
    assert html_text.count("src=\"cid:") == 1
    assert html_text.count("src='cid:") == 1
    assert html_text.count("Same body wording.") == 1
    assert {part.get_payload(decode=True) for part in images} == {_PNG_A, _PNG_B}
    assert len({part["Content-ID"].strip("<>") for part in images}) == 2


def test_no_brand_images_leave_plain_and_html_text_unchanged(tmp_path: Path) -> None:
    html = '<p>No issuer image here.</p><img src="https://example.invalid/unrelated.png" alt="Unrelated">'
    message = _message(html)
    original = message.as_bytes(policy=policy.SMTP)

    assert delivery.embed_brand_logos(message, html) == html
    assert message.get_body(preferencelist=("plain",)).get_content() == "Plain body remains unchanged.\n"
    assert message.get_body(preferencelist=("html",)).get_content() == html + "\n"
    assert not _parts(message, "image/png")
    assert original == message.as_bytes(policy=policy.SMTP)


def test_unverified_or_unknown_brand_fails_before_mime_image_is_added(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    unverified = _brand(tmp_path, "EXM", _PNG_A, verified=False)
    monkeypatch.setattr(delivery, "lookup_brand", lambda ticker: unverified if ticker == "EXM" else None)
    html = '<img data-brand-ticker="EXM" src="https://exm.example/logo.svg" alt="EXM verified wordmark">'
    message = _message(html)

    with pytest.raises(delivery.BrandingError, match="issuer logo proof is incomplete"):
        delivery.embed_brand_logos(message, html)
    assert not _parts(message, "image/png")


def test_non_registry_source_is_rejected_even_when_local_asset_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    brand = _brand(tmp_path, "EXM", _PNG_A)
    monkeypatch.setattr(delivery, "lookup_brand", lambda ticker: brand)
    html = '<img data-brand-ticker="EXM" src="C:\\logos\\EXM.png" alt="EXM verified wordmark">'

    with pytest.raises(delivery.BrandingError, match="src must match"):
        delivery.embed_brand_logos(_message(html), html)


def test_repeated_message_builds_have_identical_mime_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    brand = _brand(tmp_path, "EXM", _PNG_A)
    monkeypatch.setattr(delivery, "lookup_brand", lambda ticker: brand)
    html = '<p>Stable body.</p><img data-brand-ticker="EXM" src="https://exm.example/logo.svg" alt="EXM verified wordmark">'
    config = {"email": {"from_address": "sender@example.com", "recipient": "owner@example.com"}}
    report = {"text": "Stable body.", "html": html, "generated_at": "2026-09-05T12:00:00+00:00"}

    first = delivery._make_message(
        config,
        report,
        [],
        [],
        subject="Stable",
        package_key="p" * 64,
        message_key="m" * 64,
        part_number=1,
        total_parts=1,
    )
    second = delivery._make_message(
        config,
        report,
        [],
        [],
        subject="Stable",
        package_key="p" * 64,
        message_key="m" * 64,
        part_number=1,
        total_parts=1,
    )

    assert first.as_bytes(policy=policy.SMTP) == second.as_bytes(policy=policy.SMTP)


def test_normal_template_baseline_is_fluid_when_css_is_stripped() -> None:
    template_path = Path(__file__).parents[1] / "servicing_brief" / "templates" / "brief.html.j2"
    html = Environment(autoescape=True, loader=FileSystemLoader(template_path.parent)).get_template(template_path.name).render(
        subject="PFSI review",
        company_identity={"ticker": "PFSI", "cik": "0001745916", "event": "2026-Q2", "kind": "earnings_brief"},
        ticker="PFSI",
        period="Q2 2026",
        name="PennyMac Financial Services",
        brand={
            "verified": True,
            "logo_src": "data:image/png;base64,AA==",
            "logo_alt": "PennyMac Financial Services wordmark",
            "primary_color": "#003087",
            "width": 126,
            "height": 36,
        },
        headline_main="Servicing earnings improved.",
        chart=None,
        points=[],
        rows=[],
        sources=[],
        issues=[],
    )
    soup = BeautifulSoup(html, "html.parser")
    paper = soup.select_one("table.paper")
    assert paper is not None
    assert paper.get("width") == "100%"
    assert "width:100%" in str(paper.get("style", ""))
    assert "max-width:720px" in str(paper.get("style", ""))
    content = soup.select_one("td.content")
    assert content is not None
    assert "padding:16px" in str(content.get("style", ""))
    headline = soup.select_one("h1.headline")
    assert headline is not None
    assert "font-size:24px" in str(headline.get("style", ""))
    assert "padding:16px" in str(soup.select_one(".finding-band").get("style", ""))
    style = soup.find("style")
    assert style is not None
    desktop_css = style.get_text()
    assert "@media only screen and (max-width: 680px)" in desktop_css
    assert ".paper { width: 100% !important; max-width: 100% !important; }" in desktop_css
    assert ".content { padding: 16px !important; }" in desktop_css
    assert ".headline { font-size: 26px !important;" in desktop_css
    assert "@media only screen and (min-width: 681px)" in desktop_css
    assert ".content { padding: 28px !important; }" in desktop_css
    assert ".headline { font-size: 28px !important; }" in desktop_css
    assert "PennyMac Financial Services" in soup.get_text(" ", strip=True)
    assert soup.select_one('img[data-brand-ticker="PFSI"]') is not None


def test_decorative_logo_requires_readable_issuer_identity_and_verified_description(tmp_path, monkeypatch):
    brand = _brand(tmp_path, 'EXM', _PNG_A)
    monkeypatch.setattr(delivery, 'lookup_brand', lambda ticker: brand)
    html = ('<section data-brief-company="EXM"><table class="issuer-masthead"><tr><td>'
            '<p class="issuer-name">EXM Mortgage Holdings</p></td><td>'
            '<img data-brand-ticker="EXM" data-brand-logo-description="EXM verified wordmark" '
            'src="https://exm.example/logo.svg" alt="" aria-hidden="true">'
            '</td></tr></table></section>')
    message = _message(html)
    delivery.embed_brand_logos(message, html)
    assert len(_parts(message, 'image/png')) == 1
    for bad in (html.replace('EXM Mortgage Holdings', ''),
                html.replace('data-brand-logo-description="EXM verified wordmark"', 'data-brand-logo-description="wrong issuer"'),
                html.replace('class="issuer-name"', 'class="issuer-name" hidden')):
        with pytest.raises(delivery.BrandingError):
            delivery.embed_brand_logos(_message(bad), bad)
