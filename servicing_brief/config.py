"""One TOML file for settings and watchlist; credentials stay in environment."""
from copy import deepcopy
from pathlib import Path
import re
import tomllib
from zoneinfo import ZoneInfo


class ConfigError(ValueError):
    pass


def load_config(path: str | Path = "config.toml") -> dict:
    path = Path(path).resolve()
    if not path.exists():
        raise ConfigError(f"Configuration not found: {path}. Copy config.example.toml to config.toml first.")
    with path.open("rb") as stream:
        config = tomllib.load(stream)
    return validate_config(config, path.parent)


def validate_config(config: dict, root: Path) -> dict:
    config = deepcopy(config)
    for section in ("storage", "sources", "email", "ai", "schedule"):
        config.setdefault(section, {})
    config["_root"] = str(root.resolve())
    config["_storage"] = str((root / config["storage"].get("path", "data")).resolve())
    email = config["email"]
    if any(email.get(key) for key in ("password", "smtp_password", "token", "secret", "app_password")):
        raise ConfigError("Keep email credentials in environment variables, never in TOML.")
    if any(email.get(key) for key in ("recipients", "to")):
        raise ConfigError("Use email.recipient for one explicit recipient; recipient lists and aliases are unsupported.")
    if config["ai"].get("api_key"):
        raise ConfigError("Keep OPENAI_API_KEY in the environment, never in TOML.")
    recipient = email.get("recipient", "")
    sender = email.get("sender", "")
    for label, address in (("recipient", recipient), ("sender", sender)):
        if address and (not re.fullmatch(r"[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+", address) or "\r" in address or "\n" in address):
            raise ConfigError(f"email.{label} must be one explicit plain email address.")
    size = email.setdefault("max_message_bytes", 15 * 1024 * 1024)
    if not isinstance(size, int) or size < 8192:
        raise ConfigError("email.max_message_bytes must be an integer >= 8192.")
    schedule = config["schedule"]
    schedule.setdefault("timezone", "America/New_York")
    schedule.setdefault("times", ["07:00", "18:00"])
    try:
        ZoneInfo(schedule["timezone"])
    except Exception as exc:
        raise ConfigError("Invalid schedule.timezone") from exc
    if not schedule["times"] or any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", x) for x in schedule["times"]):
        raise ConfigError("schedule.times must contain HH:MM values.")
    companies = config.setdefault("companies", [])
    enabled_ciks = set()
    for company in companies:
        for key in ("ticker", "name", "cik"):
            if not company.get(key):
                raise ConfigError(f"Each company requires {key}")
        company["cik"] = str(company["cik"]).zfill(10)
        if not re.fullmatch(r"\d{10}", company["cik"]):
            raise ConfigError(f"Invalid CIK for {company['ticker']}")
        if company.get("enabled", False):
            if company["cik"] in enabled_ciks:
                raise ConfigError("Duplicate enabled reporting CIK; consolidate brands under the reporting entity.")
            enabled_ciks.add(company["cik"])
    config["sources"].setdefault("lookback_days", 120)
    config["sources"].setdefault("overlap_days", 14)
    return config
