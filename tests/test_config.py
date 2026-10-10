from __future__ import annotations

from pathlib import Path

import pytest

from servicing_brief.config import ConfigError, load_config, validate_config


def _company(**overrides: object) -> dict[str, object]:
    company: dict[str, object] = {
        "ticker": "TST",
        "name": "Test issuer",
        "cik": "123",
        "enabled": True,
    }
    company.update(overrides)
    return company


def test_minimal_config_keeps_defaults_and_disabled_missing_flag(tmp_path: Path) -> None:
    company = _company()
    del company["enabled"]
    config = validate_config({"companies": [company]}, tmp_path)

    assert config["companies"][0]["cik"] == "0000000123"
    assert config["companies"][0]["enabled"] is False


def test_source_and_schedule_limits_remain_subject_to_consumer_clamping(tmp_path: Path) -> None:
    config = validate_config({
        "sources": {"max_ir_document_bytes": 200 * 1024 * 1024},
        "schedule": {"catch_up_days": 60, "max_pending": 64},
    }, tmp_path)
    assert config["sources"]["max_ir_document_bytes"] == 200 * 1024 * 1024
    assert config["schedule"]["catch_up_days"] == 60
    assert config["schedule"]["max_pending"] == 64


def test_numeric_overflow_is_a_field_specific_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"email\.smtp_timeout_seconds must be a finite number"):
        validate_config({"email": {"smtp_timeout_seconds": 10 ** 400}}, tmp_path)


def test_minimal_config_defaults_without_companies(tmp_path: Path) -> None:
    config = validate_config({}, tmp_path)

    assert config["companies"] == []
    assert config["storage"]["path"] == "data"
    assert config["sources"]["lookback_days"] == 120
    assert config["sources"]["overlap_days"] == 14
    assert "bootstrap_lookback_days" not in config["sources"]
    assert config["email"]["max_message_bytes"] == 15 * 1024 * 1024
    assert config["schedule"]["timezone"] == "America/New_York"
    assert config["schedule"]["times"] == ["07:00", "18:00"]
    assert config["schedule"]["enabled"] is False
    assert config["schedule"]["catch_up"] is True


def test_company_without_enabled_flag_is_disabled(tmp_path: Path) -> None:
    config = validate_config({"companies": [_company(enabled=True)]}, tmp_path)
    del config["companies"][0]["enabled"]
    config = validate_config(config, tmp_path)

    assert config["companies"][0]["enabled"] is False


def test_example_configuration_is_backward_compatible() -> None:
    config = load_config(Path(__file__).parents[1] / "config.example.toml")

    assert {company["ticker"] for company in config["companies"]} >= {"TFC", "PFSI", "WFC", "RKT"}
    assert all(isinstance(company["enabled"], bool) for company in config["companies"])
    assert any(company["enabled"] is False for company in config["companies"])
    assert config["email"]["sender"] == ""
    assert config["email"]["recipient"] == ""


@pytest.mark.parametrize("value", [None, [], "config", 1])
def test_configuration_root_must_be_a_dict(value: object, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Configuration root"):
        validate_config(value, tmp_path)  # type: ignore[arg-type]


@pytest.mark.parametrize("section", ["storage", "sources", "email", "ai", "schedule", "analysis"])
def test_named_sections_must_be_tables(section: str, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=section):
        validate_config({section: []}, tmp_path)


@pytest.mark.parametrize("companies", [None, {}, "TST", ["TST"]])
def test_companies_must_be_a_list_of_tables(companies: object, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="companies"):
        validate_config({"companies": companies}, tmp_path)


@pytest.mark.parametrize("field", ["ticker", "name", "cik"])
@pytest.mark.parametrize("value", [None, True, []])
def test_company_identity_fields_require_nonempty_strings(field: str, value: object, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=rf"companies\[0\]\.{field}"):
        validate_config({"companies": [_company(**{field: value})]}, tmp_path)


def test_company_enabled_rejects_truthy_string(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"companies\[0\]\.enabled"):
        validate_config({"companies": [_company(enabled="false")]}, tmp_path)


def test_cik_must_be_digits_and_is_zero_padded(tmp_path: Path) -> None:
    config = validate_config({"companies": [_company(cik="92230")]}, tmp_path)
    assert config["companies"][0]["cik"] == "0000092230"
    integer_config = validate_config({"companies": [_company(cik=92230)]}, tmp_path)
    assert integer_config["companies"][0]["cik"] == "0000092230"
    with pytest.raises(ConfigError, match=r"companies\[0\]\.cik"):
        validate_config({"companies": [_company(cik="12345678901")]}, tmp_path)


def test_storage_path_must_be_nonempty_string(tmp_path: Path) -> None:
    for value in (None, 7, "", "   ", "\x00"):
        with pytest.raises(ConfigError, match="storage.path"):
            validate_config({"storage": {"path": value}}, tmp_path)


@pytest.mark.parametrize("field", ["sender", "recipient"])
def test_email_addresses_require_strings_and_one_plain_address(field: str, tmp_path: Path) -> None:
    for value in (None, 7, ["owner@example.com"], "owner@example.com,other@example.com"):
        with pytest.raises(ConfigError, match=rf"email\.{field}"):
            validate_config({"email": {field: value}}, tmp_path)


@pytest.mark.parametrize("field", ["enabled", "catch_up"])
def test_schedule_boolean_fields_reject_truthy_strings(field: str, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=rf"schedule\.{field}"):
        validate_config({"schedule": {field: "false"}}, tmp_path)


def test_schedule_timezone_and_times_are_field_specific(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="schedule.timezone"):
        validate_config({"schedule": {"timezone": 5}}, tmp_path)
    with pytest.raises(ConfigError, match="schedule.timezone"):
        validate_config({"schedule": {"timezone": "Mars/Luna"}}, tmp_path)
    for value in ("07:00", ["07:00", 8], ["7:00"], []):
        with pytest.raises(ConfigError, match="schedule.times"):
            validate_config({"schedule": {"times": value}}, tmp_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lookback_days", 0),
        ("lookback_days", True),
        ("overlap_days", -1),
        ("overlap_days", False),
        ("bootstrap_lookback_days", 0),
        ("bootstrap_lookback_days", "450"),
    ],
)
def test_source_windows_require_bounded_integer_types(field: str, value: object, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=rf"sources\.{field}"):
        validate_config({"sources": {field: value}}, tmp_path)


@pytest.mark.parametrize("value", [True, False, 0, -1, 8191, 12.5])
def test_max_message_bytes_is_a_positive_nonboolean_range(value: object, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="email.max_message_bytes"):
        validate_config({"email": {"max_message_bytes": value}}, tmp_path)


def test_optional_analysis_section_accepts_catalog_path(tmp_path: Path) -> None:
    config = validate_config({"analysis": {"catalog_path": "reviewed.json"}}, tmp_path)
    assert config["analysis"]["catalog_path"] == "reviewed.json"


def test_load_config_converts_toml_and_read_failures_without_echoing_content(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    secret = "do-not-print-this-secret"
    bad.write_text(f'[email\nsender = "{secret}"', encoding="utf-8")
    with pytest.raises(ConfigError) as error:
        load_config(bad)
    assert "invalid TOML" in str(error.value)
    assert secret not in str(error.value)

    with pytest.raises(ConfigError, match="could not be read"):
        load_config(tmp_path)
