"""Windows-friendly entry points. Network sends always require an explicit command flag."""
import argparse
import importlib.util
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
from .config import load_config, ConfigError
from .state import State


def configure_logging(storage: Path):
    storage.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(storage / "brief.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger = logging.getLogger("servicing_brief")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    # Third-party debug logs could expose SEC identity, HTTP headers or SMTP credentials.
    for name in ("edgar", "httpx", "httpcore", "openai"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


def doctor(config, *, offline=False):
    storage = Path(config["_storage"])
    checks = []
    try:
        storage.mkdir(parents=True, exist_ok=True)
        probe = storage / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append({"check": "storage", "status": "ok", "path": str(storage)})
    except OSError:
        checks.append({"check": "storage", "status": "failed", "detail": "Storage is not writable"})
    for variable in ("EDGAR_IDENTITY", "OPENAI_API_KEY"):
        checks.append({"check": variable, "configured": "yes" if os.getenv(variable) else "no"})
    checks.append({"check": "recipient", "configured": "yes" if config["email"].get("recipient") else "no"})
    checks.append({"check": "sender", "configured": "yes" if config["email"].get("sender") else "no"})
    checks.append({"check": "optional_ai", "enabled": bool(config["ai"].get("enabled", False)), "sdk_installed": importlib.util.find_spec("openai") is not None,
                   "detail": "Evidence-only reports remain available without AI."})
    if not offline:
        from . import sources
        if hasattr(sources, "doctor_sources"):
            try:
                checks.extend(sources.doctor_sources(config))
            except Exception as exc:
                checks.append({"check": "sources", "status": "failed", "detail": type(exc).__name__})
        else:
            checks.append({"check": "sources", "status": "not_tested", "detail": "Run bootstrap --dry-run for a full collection check."})
    else:
        checks.append({"check": "network", "status": "not_tested", "detail": "Offline doctor requested."})
    from .delivery import doctor_email
    checks.extend(doctor_email(config))
    return {"checks": checks, "live_delivery": "not_tested_by_doctor", "schedule": "activation_is_separate"}


def parser():
    result = argparse.ArgumentParser(prog="servicing-brief", description="Mortgage Servicing Earnings Brief")
    result.add_argument("--config", default="config.toml", help="Single TOML settings/watchlist file")
    commands = result.add_subparsers(dest="command", required=True)
    check = commands.add_parser("doctor", help="Check configuration, storage and source access; never send")
    check.add_argument("--offline", action="store_true")
    for name in ("bootstrap", "run-once", "scheduled"):
        cmd = commands.add_parser(name)
        mode = cmd.add_mutually_exclusive_group()
        mode.add_argument("--dry-run", action="store_true", help="Write HTML and EML previews (default)")
        mode.add_argument("--send", action="store_true", help="Send to the explicitly configured recipient")
        cmd.add_argument("--offline", action="store_true", help="Replay local archive only; no source freshness check")
    send = commands.add_parser("send-test", help="Send one explicit test to configured recipient")
    send.add_argument("--send", action="store_true", required=True, help="Required explicit authorization to send")
    reconcile = commands.add_parser("reconcile", help="Record an explicit provider check after ambiguous acceptance; never sends")
    reconcile.add_argument("--message-key", required=True)
    reconcile.add_argument("--decision", choices=("accepted", "retry"), required=True)
    reconcile.add_argument("--detail", default="", help="Optional note describing the provider check")
    commands.add_parser("status", help="Read local collection, briefing, delivery and schedule status")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        config = load_config(args.config)
        storage = Path(config["_storage"])
        configure_logging(storage)
        if args.command == "doctor":
            response = doctor(config, offline=args.offline)
        elif args.command == "status":
            state = State(storage / "state.sqlite3")
            try:
                response = state.status()
                from . import delivery
                if hasattr(delivery, "delivery_status"):
                    response["delivery"] = delivery.delivery_status(state.path)
                from . import scheduling
                if hasattr(scheduling, "schedule_status"):
                    response["schedule"] = scheduling.schedule_status(config, state.path)
            finally:
                state.close()
        elif args.command == "send-test":
            from .delivery import send_test
            config["_send"] = True
            response = send_test(config, storage / "state.sqlite3")
        elif args.command == "reconcile":
            from filelock import FileLock
            from .delivery import reconcile_message
            state_db = storage / "state.sqlite3"
            with FileLock(str(state_db) + ".delivery.lock", timeout=0):
                response = reconcile_message(state_db, args.message_key, decision=args.decision, detail=args.detail)
        elif args.command == "scheduled":
            from .scheduling import scheduled_run
            from .pipeline import run
            response = scheduled_run(config, storage / "state.sqlite3", lambda: run(config, send=args.send, offline=args.offline))
        else:
            from .pipeline import run
            response = run(config, bootstrap=args.command == "bootstrap", send=args.send, offline=args.offline)
        print(json.dumps(response, indent=2, ensure_ascii=True))
        if isinstance(response, dict) and response.get("status") in ("delivery_incomplete", "collection_incomplete", "partial_failure", "failed"):
            return 2
        if isinstance(response, dict) and response.get("successful") is False:
            return 2
        if isinstance(response, list) and any(item.get("status") not in {"accepted", "already_accepted"} for item in response):
            return 2
        return 0
    except ConfigError as exc:
        print(json.dumps({"status": "configuration_error", "detail": str(exc)}), file=sys.stderr)
        return 2
    except Exception as exc:
        # Deliberately avoid arbitrary exception text from external transports.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__, "detail": "Check configuration, doctor and local state/logs. No success has been recorded for this operation."}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
