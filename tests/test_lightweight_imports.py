"""Focused regressions for deferred optional and stateful imports."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import textwrap
import types

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _isolated_python(source: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"isolated process failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def test_sources_package_defers_backends_and_preserves_export_patches() -> None:
    _isolated_python(
        """
        import sys

        import servicing_brief.sources as sources

        assert "servicing_brief.sources.collector" not in sys.modules
        assert "servicing_brief.sources.ir" not in sys.modules
        assert "servicing_brief.sources.sec" not in sys.modules
        assert "httpx" not in sys.modules
        assert "bs4" not in sys.modules
        assert "filelock" not in sys.modules

        from servicing_brief.sources import collector, ir, sec
        from servicing_brief.sources import collect, discover_ir, discover_sec

        assert collect is collector.collect
        assert discover_ir is ir.discover_ir
        assert discover_sec is sec.discover_sec
        replacement = object()
        collector.collect = replacement
        assert sources.collect is replacement
        """
    )


def test_branding_defers_pymupdf_until_png_decode() -> None:
    _isolated_python(
        """
        import sys

        import servicing_brief.branding as branding

        assert "pymupdf" not in sys.modules
        assert branding._png_dimensions(bytes.fromhex("89504e470d0a1a0a")) is None
        assert "pymupdf" in sys.modules
        """
    )


def test_cli_import_defers_config_and_state() -> None:
    _isolated_python(
        """
        import sys

        import servicing_brief.cli as cli

        assert "servicing_brief.config" not in sys.modules
        assert "servicing_brief.state" not in sys.modules
        cli.parser()
        assert "servicing_brief.config" not in sys.modules
        assert "servicing_brief.state" not in sys.modules
        """
    )


def test_cli_load_config_patch_and_configuration_diagnostic_are_preserved(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from servicing_brief import cli

    error_type = cli.ConfigError
    monkeypatch.setattr(cli, "load_config", lambda path: (_ for _ in ()).throw(error_type("bad config")))

    assert cli.main(["doctor", "--offline"]) == 2
    diagnostic = json.loads(capsys.readouterr().err)
    assert diagnostic == {"status": "configuration_error", "detail": "bad config"}


def test_offline_sec_check_is_lightweight_and_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    from servicing_brief import cli

    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name: None if name == "edgar" else None)
    missing = cli._sec_backend_check({})
    assert missing["status"] == "failed"
    assert "uv sync --extra sec" in missing["detail"]


def test_offline_doctor_includes_sec_backend_check_without_importing_edgar(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from servicing_brief import cli

    fake_delivery = types.ModuleType("servicing_brief.delivery")
    fake_delivery.doctor_email = lambda config: []
    monkeypatch.setitem(sys.modules, "servicing_brief.delivery", fake_delivery)
    monkeypatch.delitem(sys.modules, "edgar", raising=False)
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name: None if name == "edgar" else None)
    config = {
        "_storage": str(tmp_path),
        "email": {},
        "ai": {},
    }

    result = cli.doctor(config, offline=True)

    sec_check = next(check for check in result["checks"] if check["check"] == "sec_backend")
    assert sec_check["status"] == "failed"
    assert "edgar" not in sys.modules
