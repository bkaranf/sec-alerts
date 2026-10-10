"""Doctor exit status reflects probe results without altering stored files."""
import json
import logging
from types import ModuleType
import sys

import pytest

from servicing_brief import cli


@pytest.fixture
def diagnostic_config(tmp_path, monkeypatch):
    config = {"_storage": str(tmp_path), "email": {}, "ai": {}}
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name: object())
    delivery = ModuleType("servicing_brief.delivery")
    delivery.doctor_email = lambda config: [
        {"check": "recipient", "configured": False},
        {"check": "sender", "configured": False},
        {"check": "send_gate", "configured": False},
    ]
    monkeypatch.setitem(sys.modules, "servicing_brief.delivery", delivery)
    return config


def test_offline_doctor_preserves_existing_probe_and_closes_its_log(diagnostic_config, tmp_path, capsys):
    existing = tmp_path / ".write-probe"
    existing.write_bytes(b"existing local data")
    logger = logging.getLogger("servicing_brief")
    initial_handlers = list(logger.handlers)
    for _ in range(2):
        assert cli.main(["doctor", "--offline"]) == 0
        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "ok"
        assert output["live_delivery"] == "not_tested_by_doctor"
        assert [item["check"] for item in output["checks"]].count("sender") == 1
        assert logger.handlers == initial_handlers
    assert existing.read_bytes() == b"existing local data"
    assert list(tmp_path.glob(".write-probe-*")) == []


def test_missing_sec_backend_returns_failure_exit_status(diagnostic_config, monkeypatch, capsys):
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name: None)
    assert cli.main(["doctor", "--offline"]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "failed"
    assert any(item.get("check") == "sec_backend" and item.get("status") == "failed" for item in output["checks"])


@pytest.mark.parametrize("diagnostic", [
    {"source": "sec", "access_status": "blocked_lock"},
    {"source": "sec", "access_status": "dependency_missing"},
    {"ticker": "TFC", "enabled": True, "ir": {"access_status": "blocked"}},
    {"ticker": "TFC", "enabled": True, "ir": {"access_status": "unexpected_status"}},
])
def test_live_doctor_nested_source_failure_has_nonzero_exit(diagnostic_config, diagnostic, monkeypatch, capsys):
    from servicing_brief import sources
    monkeypatch.setattr(sources, "doctor_sources", lambda config: [diagnostic])
    assert cli.main(["doctor"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "failed"


def test_disabled_issuer_and_unconfigured_optional_delivery_are_informational(diagnostic_config, monkeypatch, capsys):
    from servicing_brief import sources
    monkeypatch.setattr(sources, "doctor_sources", lambda config: [
        {"source": "sec", "access_status": "ok"},
        {"ticker": "DISABLED", "enabled": False, "ir": {"access_status": "not_configured"}},
    ])
    assert cli.main(["doctor"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"


def test_storage_probe_failure_is_reported_without_exception_details(diagnostic_config, monkeypatch):
    def denied(**kwargs):
        raise PermissionError("private contents must not be echoed")
    monkeypatch.setattr(cli.tempfile, "TemporaryFile", denied)
    result = cli.doctor(diagnostic_config, offline=True)
    assert result["status"] == "failed"
    assert result["checks"][0]["status"] == "failed"
    assert "private contents" not in json.dumps(result)
