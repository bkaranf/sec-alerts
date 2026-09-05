"""The issuer's palette, rather than a previous brief's fill, controls its page."""
import pytest
from servicing_brief.branding import BrandRegistryError, brand_view, contrast_ratio, validate_page_theme


def surface_html(ticker):
    t = brand_view(ticker, ticker)['theme']
    return (f'<body data-brand-surface="page" style="background:{t["page_bg"]}">'
            f'<main data-brand-surface="paper" style="background:{t["paper_bg"]}">'
            f'<section class="finding-band" data-brand-surface="hero" style="background:{t["hero_bg"]};color:{t["hero_text"]}">'
            f'<h1 style="color:{t["hero_text"]}">Company result</h1><p style="color:{t["hero_text"]}">Finding '
            f'<a href="https://issuer.example/results" style="color:{t["hero_text"]}">[1]</a></p></section></main></body>')


@pytest.mark.parametrize('ticker', ['TD', 'RY', 'CM', 'BNS', 'BMO', 'PFSI', 'UNKNOWN'])
def test_whole_page_palette_and_contrast(ticker):
    b = brand_view(ticker, ticker)
    t = b['theme']
    assert t['hero_bg'] == b['primary_color'].upper()
    assert contrast_ratio(t['hero_bg'], t['hero_text']) >= 4.5
    for surface in ('paper_bg', 'section_bg', 'page_bg'):
        assert contrast_ratio(t[surface], t['heading_color']) >= 4.5
    validate_page_theme(surface_html(ticker), ticker)


def test_wrong_company_panel_or_page_is_rejected():
    html = surface_html('TD')
    td = brand_view('TD', 'TD')['theme']
    rbc = brand_view('RY', 'RY')['theme']
    for key in ('hero_bg', 'page_bg', 'paper_bg'):
        with pytest.raises(BrandRegistryError):
            validate_page_theme(html.replace(td[key], rbc[key]), 'TD')


def test_fallback_citation_contrast_and_missing_theme_are_rejected():
    html = surface_html('PFSI')
    with pytest.raises(BrandRegistryError, match='citation'):
        validate_page_theme(html.replace('style="color:#FFFFFF">[1]', 'style="color:#183f51">[1]'), 'PFSI')
    with pytest.raises(BrandRegistryError, match='requires'):
        validate_page_theme('<body><h1>Unstyled brief</h1></body>', 'PFSI')
