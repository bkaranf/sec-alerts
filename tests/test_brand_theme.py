"""The issuer's palette, rather than a previous brief's fill, controls its page."""
import pytest
from servicing_brief.branding import BrandRegistryError, apply_page_theme, brand_view, contrast_ratio, page_theme, validate_page_theme, validate_theme_contrast


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


@pytest.mark.parametrize('ticker', ['TD', 'RY', 'CM', 'BNS', 'BMO', 'PFSI', 'UNKNOWN'])
def test_semantic_light_and_dark_palettes_have_readable_text_and_charts(ticker):
    theme = brand_view(ticker, ticker)['theme']
    validate_theme_contrast(theme)
    for prefix in ('', 'dark_'):
        for surface in ('page_bg', 'paper_bg', 'section_bg'):
            assert contrast_ratio(theme[prefix+'body_color'], theme[prefix+surface]) >= 7
            assert contrast_ratio(theme[prefix+'secondary_color'], theme[prefix+surface]) >= 4.5
        for mark in ('prior_bar_color', 'current_bar_color', 'zero_color'):
            assert contrast_ratio(theme[prefix+mark], theme[prefix+'section_bg']) >= 3
        for signal in ('positive', 'negative'):
            assert contrast_ratio(theme[prefix+signal+'_color'], theme[prefix+signal+'_bg']) >= 7


@pytest.mark.parametrize('primary', ['#FFFFFF', '#000000', '#FFFF00', '#00FFFF', '#FF00FF', '#FF0000', '#00FF00', '#0000FF', '#808080'])
def test_future_extreme_primary_colors_keep_exact_hero_and_pass_all_pairs(primary):
    theme = page_theme({'primary_color': primary, 'accent_color': '#FFFFFF'})
    assert theme['hero_bg'] == primary
    validate_theme_contrast(theme)


def test_low_contrast_semantic_colors_fail_closed():
    theme = brand_view('PFSI', 'PennyMac')['theme']
    for key in ('secondary_color', 'prior_bar_color', 'zero_color', 'dark_negative_color'):
        broken = dict(theme)
        broken[key] = theme['dark_negative_bg'] if key.startswith('dark') else theme['section_bg']
        with pytest.raises(BrandRegistryError, match='contrast'):
            validate_theme_contrast(broken)


def test_email_theme_survives_style_removal_and_dark_rules_bind_inline_pairs():
    from bs4 import BeautifulSoup
    theme = brand_view('PFSI', 'PennyMac')['theme']
    source = surface_html('PFSI').replace('</main>',
        f'<table style="background:{theme["section_bg"]};color:{theme["body_color"]}"><tr>'
        f'<td style="background:{theme["negative_bg"]};color:{theme["negative_color"]}">($77m)</td>'
        f'<td style="color:{theme["secondary_color"]}">$57m</td></tr></table></main>')
    html = apply_page_theme(source, theme)
    soup = BeautifulSoup(html, 'html.parser')
    assert '(prefers-color-scheme: dark)' in html
    assert theme['dark_negative_color'] in html
    assert soup.select_one('table')['bgcolor'] == theme['section_bg']
    assert soup.select_one('td')['bgcolor'] == theme['negative_bg']
    assert apply_page_theme(html, theme) == html
    for style in soup.find_all('style'):
        style.decompose()
    validate_page_theme(str(soup), 'PFSI')
    assert '($77m)' in soup.get_text()
