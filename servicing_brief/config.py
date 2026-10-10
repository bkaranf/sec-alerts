"""Load and validate the single TOML settings and watchlist file."""
from copy import deepcopy
import math
from pathlib import Path
import re
import tomllib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(ValueError):
    """Raised when a configuration file cannot be loaded or validated."""


_PLAIN_EMAIL = re.compile(r"[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+\Z")
_CLOCK = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d\Z")
_DEFAULT_MAX_MESSAGE_BYTES = 15 * 1024 * 1024
_MIN_MESSAGE_BYTES = 8192


def _section(config: dict, name: str) -> dict:
    """Return a named TOML table, raising a field-specific error otherwise."""

    if name not in config:
        value = {}
        config[name] = value
    else:
        value = config[name]
    if not isinstance(value, dict):
        raise ConfigError(f"config.{name} must be a table/object.")
    return value


def _require_string(section: dict, field: str, *, allow_empty: bool = True, label: str | None = None) -> str | None:
    """Validate an optional string field and return its value when present."""

    label = label or field
    if field not in section:
        return None
    value = section[field]
    if not isinstance(value, str):
        raise ConfigError(f"{label} must be a string.")
    if not allow_empty and not value.strip():
        raise ConfigError(f"{label} must be a non-empty string.")
    return value


def _require_bool(section: dict, field: str, *, label: str | None = None) -> bool | None:
    """Validate an optional boolean field without accepting truthy strings."""

    label = label or field
    if field not in section:
        return None
    value = section[field]
    if not isinstance(value, bool):
        raise ConfigError(f"{label} must be a boolean (true or false).")
    return value


def _require_int(section: dict, field: str, *, minimum: int, maximum: int | None = None, label: str | None = None) -> int | None:
    """Validate an optional integer setting, excluding Python booleans."""

    label = label or field
    if field not in section:
        return None
    value = section[field]
    if isinstance(value, bool) or not isinstance(value, int):
        if maximum is None:
            bounds = f"at least {minimum}"
        else:
            bounds = f"between {minimum} and {maximum}"
        raise ConfigError(f"{label} must be an integer {bounds}.")
    if value < minimum or (maximum is not None and value > maximum):
        if maximum is None:
            raise ConfigError(f"{label} must be an integer at least {minimum}.")
        raise ConfigError(f"{label} must be an integer between {minimum} and {maximum}.")
    return value


def _require_number(section: dict, field: str, *, minimum: float, maximum: float | None = None, label: str | None = None) -> float | int | None:
    """Validate a finite numeric setting, excluding booleans."""

    label = label or field
    if field not in section:
        return None
    value = section[field]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{label} must be a number.")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ConfigError(f"{label} must be a finite number.")
    if value < minimum or (maximum is not None and value > maximum):
        if maximum is None:
            raise ConfigError(f"{label} must be at least {minimum}.")
        raise ConfigError(f"{label} must be between {minimum} and {maximum}.")
    return value


def _validate_email(email: dict) -> None:
    # Keep the existing environment-only credential rule. The error does not
    # include the value, so a mistaken secret cannot leak through diagnostics.
    if any(email.get(key) for key in ("password", "smtp_password", "token", "secret", "app_password")):
        raise ConfigError("Keep email credentials in environment variables, never in TOML.")
    if any(email.get(key) for key in ("recipients", "to")):
        raise ConfigError("Use email.recipient for one explicit recipient; recipient lists and aliases are unsupported.")

    for field in ("sender", "recipient", "from_address", "from"):
        value = _require_string(email, field, label=f"email.{field}")
        if value and not _PLAIN_EMAIL.fullmatch(value):
            raise ConfigError(f"email.{field} must be one explicit plain email address.")

    for field in ("smtp_host", "smtp_security", "smtp_username", "smtp_username_env", "smtp_password_env"):
        _require_string(email, field, label=f"email.{field}")
    _require_bool(email, "enabled", label="email.enabled")
    _require_bool(email, "send_enabled", label="email.send_enabled")
    _require_int(email, "smtp_port", minimum=1, maximum=65535, label="email.smtp_port")
    _require_number(email, "smtp_timeout_seconds", minimum=0.001, label="email.smtp_timeout_seconds")

    email.setdefault("max_message_bytes", _DEFAULT_MAX_MESSAGE_BYTES)
    _require_int(
        email,
        "max_message_bytes",
        minimum=_MIN_MESSAGE_BYTES,
        label="email.max_message_bytes",
    )

    if email.get("api_key"):
        raise ConfigError("Keep OPENAI_API_KEY in the environment, never in TOML.")


def _validate_sources(sources: dict) -> None:
    # These are the settings consumed by SEC/IR acquisition. Their bounds
    # mirror the consumer defaults and avoid a later int()/float() failure.
    for field, minimum in (
        ("lookback_days", 1),
        ("overlap_days", 0),
        ("bootstrap_lookback_days", 1),
        ("max_sec_filings_per_company", 1),
        ("max_relevant_8k_per_company", 1),
        ("max_periodic_exhibits", 0),
        ("max_assets_per_exhibit", 1),
        ("max_ir_pages", 1),
        ("max_ir_documents", 1),
        ("max_ir_retries", 0),
        ("ir_max_retries", 0),
    ):
        _require_int(sources, field, minimum=minimum, label=f"sources.{field}")
    _require_int(sources, "max_ir_document_bytes", minimum=1024, label="sources.max_ir_document_bytes")
    _require_number(sources, "ir_timeout", minimum=0.001, label="sources.ir_timeout")
    _require_number(sources, "lock_timeout", minimum=0, label="sources.lock_timeout")
    _require_string(sources, "ir_user_agent", label="sources.ir_user_agent")
    _require_string(sources, "sec_lock_path", label="sources.sec_lock_path")


def _validate_ai(ai: dict) -> None:
    _require_bool(ai, "enabled", label="ai.enabled")
    for field in ("model", "prompt_version", "api_key_env", "cache_dir"):
        _require_string(ai, field, label=f"ai.{field}")
    # The narrative worker clamps these values to its hard caps. Validate their
    # type and nonnegative shape here while retaining that documented behavior
    # for larger values supplied by an existing configuration.
    _require_int(ai, "max_requests_per_run", minimum=0, label="ai.max_requests_per_run")
    _require_int(ai, "max_requests", minimum=0, label="ai.max_requests")
    _require_int(ai, "max_input_chars", minimum=0, label="ai.max_input_chars")
    _require_int(ai, "max_output_chars", minimum=0, label="ai.max_output_chars")
    _require_int(ai, "retries", minimum=0, label="ai.retries")
    if ai.get("api_key"):
        raise ConfigError("Keep OPENAI_API_KEY in the environment, never in TOML.")


def _validate_schedule(schedule: dict) -> None:
    schedule.setdefault("timezone", "America/New_York")
    timezone_name = schedule["timezone"]
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise ConfigError("schedule.timezone must be a valid IANA timezone string.")
    try:
        ZoneInfo(timezone_name)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ConfigError("schedule.timezone must be a valid IANA timezone string.") from exc

    schedule.setdefault("times", ["07:00", "18:00"])
    times = schedule["times"]
    if not isinstance(times, list) or not times:
        raise ConfigError("schedule.times must be a non-empty list of HH:MM strings.")
    for index, value in enumerate(times):
        if not isinstance(value, str) or not _CLOCK.fullmatch(value):
            raise ConfigError(f"schedule.times[{index}] must be an HH:MM string.")

    _require_bool(schedule, "enabled", label="schedule.enabled")
    _require_bool(schedule, "catch_up", label="schedule.catch_up")
    _require_int(schedule, "catch_up_days", minimum=0, label="schedule.catch_up_days")
    _require_int(schedule, "max_pending", minimum=1, label="schedule.max_pending")


def _validate_analysis(analysis: dict) -> None:
    _require_string(analysis, "catalog_path", label="analysis.catalog_path")


def _validate_companies(config: dict) -> None:
    if "companies" not in config:
        companies = []
        config["companies"] = companies
    else:
        companies = config["companies"]
    if not isinstance(companies, list):
        raise ConfigError("companies must be a list of company tables.")

    enabled_ciks: set[str] = set()
    for index, company in enumerate(companies):
        prefix = f"companies[{index}]"
        if not isinstance(company, dict):
            raise ConfigError(f"{prefix} must be a table/object.")
        for key in ("ticker", "name"):
            value = company.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"{prefix}.{key} must be a non-empty string.")
        if "enabled" in company:
            _require_bool(company, "enabled", label=f"{prefix}.enabled")
        else:
            # A missing flag must stay disabled at the pipeline boundary. This
            # prevents a source collector from treating a partial entry as an
            # enabled issuer.
            company["enabled"] = False

        raw_cik = company.get("cik")
        if isinstance(raw_cik, bool):
            raise ConfigError(f"{prefix}.cik must be a positive integer or digit string.")
        if isinstance(raw_cik, int):
            if raw_cik <= 0 or raw_cik > 9_999_999_999:
                raise ConfigError(f"{prefix}.cik must be a positive integer or digit string.")
            cik = str(raw_cik)
        elif isinstance(raw_cik, str) and re.fullmatch(r"\d{1,10}\Z", raw_cik):
            cik = raw_cik
        else:
            raise ConfigError(f"{prefix}.cik must be a positive integer or digit string.")
        company["cik"] = cik.zfill(10)
        if company["enabled"]:
            if company["cik"] in enabled_ciks:
                raise ConfigError("Duplicate enabled reporting CIK; consolidate brands under the reporting entity.")
            enabled_ciks.add(company["cik"])


def load_config(path: str | Path = "config.toml") -> dict:
    """Load one TOML file and convert file/parser failures to ``ConfigError``."""

    try:
        config_path = Path(path).resolve()
    except (OSError, TypeError, ValueError) as exc:
        raise ConfigError("Configuration path must be a valid file path.") from exc
    if not config_path.exists():
        raise ConfigError("Configuration file was not found. Copy config.example.toml to config.toml first.")
    try:
        with config_path.open("rb") as stream:
            config = tomllib.load(stream)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError("Configuration file contains invalid TOML. Check its table and value syntax.") from exc
    except (OSError, UnicodeError) as exc:
        raise ConfigError("Configuration file could not be read. Check the path and file permissions.") from exc
    return validate_config(config, config_path.parent)


def validate_config(config: dict, root: Path) -> dict:
    """Validate and normalize a TOML-derived configuration dictionary."""

    if not isinstance(config, dict):
        raise ConfigError("Configuration root must be a table/object.")
    try:
        config = deepcopy(config)
        root_path = Path(root).resolve()
    except (OSError, TypeError, ValueError) as exc:
        raise ConfigError("Configuration root must be a valid directory path.") from exc

    storage = _section(config, "storage")
    sources = _section(config, "sources")
    email = _section(config, "email")
    ai = _section(config, "ai")
    schedule = _section(config, "schedule")
    if "analysis" in config:
        analysis = config["analysis"]
        if not isinstance(analysis, dict):
            raise ConfigError("config.analysis must be a table/object.")
        _validate_analysis(analysis)

    storage.setdefault("path", "data")
    _require_string(storage, "path", allow_empty=False, label="storage.path")
    _validate_sources(sources)
    sources.setdefault("lookback_days", 120)
    sources.setdefault("overlap_days", 14)
    _validate_email(email)
    _validate_ai(ai)
    schedule.setdefault("enabled", False)
    schedule.setdefault("catch_up", True)
    _validate_schedule(schedule)
    _validate_companies(config)

    config["_root"] = str(root_path)
    try:
        storage_path = (root_path / storage["path"]).resolve()
    except (OSError, TypeError, ValueError) as exc:
        raise ConfigError("storage.path must be a valid path.") from exc
    config["_storage"] = str(storage_path)
    return config
