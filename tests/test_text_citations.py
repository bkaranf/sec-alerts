"""Plain text explanations use compact citations while source evidence stays intact."""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader


ROOT = Path(__file__).resolve().parents[1]


def _render_text(explanations: list[dict], sources: list[dict]) -> str:
    env = Environment(
        loader=FileSystemLoader(ROOT / "servicing_brief" / "templates"),
        autoescape=False,
    )
    return env.get_template("brief.txt.j2").render(
        ticker="PFSI",
        period="Q2 2026",
        event_date_label="",
        event_date="",
        name="PennyMac Financial Services",
        headline_main="Servicing income improved.",
        headline_secondary="",
        chart=None,
        points=[],
        rows=[],
        yoy_label="YoY",
        qoq_label="QoQ",
        notes=[],
        explanations=explanations,
        selected_excerpts=[],
        reviewed_context=[],
        call_passages=[],
        questions=[],
        sources=sources,
        issues=[],
        as_of="2026-09-05",
    )


def test_explanation_citations_resolve_to_source_footer_without_internal_locators() -> None:
    source_one = "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000123/ex99-1.htm"
    source_two = "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000124/ex99-2.htm"
    explanations = [
        {
            "summary": "Management says slower housing sales affect mortgage delinquencies.",
            "topic": "Housing",
            "text": "Management says slower housing sales affect mortgage delinquencies.",
            "source_number": "1",
            "location": "HTML text line 392",
            "url": source_one,
        },
        {
            "summary": "Management described mortgage losses as low and in line with historical levels.",
            "topic": "Mortgage losses",
            "text": "Management described mortgage losses as low and in line with historical levels.",
            "source_number": "2",
            "location": "HTML text line 417",
            "url": source_two,
        },
    ]
    sources = [
        {"number": "1", "name": "PFSI earnings release", "date": "Jul 29, 2026", "url": source_one},
        {"number": "2", "name": "PFSI supplemental filing", "date": "Jul 29, 2026", "url": source_two},
    ]
    original_locations = {item["source_number"]: item["location"] for item in explanations}
    source_by_number = {source["number"]: source for source in sources}

    rendered = _render_text(explanations, sources)
    inline, source_footer = rendered.split("Sources\n", 1)

    for item in explanations:
        assert f"[{item['source_number']}]" in inline
        footer_source = source_by_number[item["source_number"]]
        assert footer_source["url"] == item["url"]
        assert footer_source["url"] in source_footer
        assert item["location"] == original_locations[item["source_number"]]

    assert "[1;" not in inline
    assert "[2;" not in inline
    assert "HTML text line" not in inline
    assert source_one not in inline
    assert source_two not in inline
    assert "—" not in rendered
