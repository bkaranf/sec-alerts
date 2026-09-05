from __future__ import annotations

import hashlib
import smtplib
from datetime import date, datetime, time, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path
from zipfile import ZipFile
from io import BytesIO

import pytest

from servicing_brief import delivery
from servicing_brief.document_copies import html_bundle
from servicing_brief.models import Document
from servicing_brief.scheduling import due, schedule_status, scheduled_run


def _config(*, send: bool = False, recipient: str = "owner@example.com", limit: int | None = None) -> dict:
    email = {
        "from_address": "sender@example.com",
        "recipient": recipient,
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 465,
        "smtp_security": "ssl",
        "smtp_username": "sender@example.com",
        "smtp_password_env": "TEST_GMAIL_APP_PASSWORD",
        "retry_attempts": 2,
    }
    if limit is not None:
        email["max_message_bytes"] = limit
    result = {"email": email}
    if send:
        result["_send"] = True
    return result


def _report(body: str = "Readout") -> dict:
    html = (
        "<html><body data-brief-company='TFC' data-brief-cik='0012345678' "
        "data-brief-event='2026-Q2'><p>" + body + "</p></body></html>"
    )
    text = f"Company: Truist Financial Corporation (TFC)\nEarnings period: Q2 2026\n\n{body}"
    return {
        "subject": "Brief",
        "text": text,
        "html": html,
        "company_identity": {
            "ticker": "TFC",
            "cik": "0012345678",
            "event": "2026-Q2",
            "kind": "earnings_brief",
        },
        "company_reports": {"TFC": {"html": html, "text": text}},
        "generated_at": "2026-08-02T12:00:00+00:00",
    }


def _document(path: Path, *, content: bytes, document_id: str = "") -> Document:
    return Document(
        issuer="TFC",
        cik="0012345678",
        title="Quarterly presentation",
        kind="presentation",
        source="ir",
        url=f"https://investor.example/{path.name}",
        published="2026-08-01T12:00:00+00:00",
        period="2026-Q2",
        path=str(path),
        content_hash=hashlib.sha256(content).hexdigest(),
        id=document_id,
        metadata={"ticker": "TFC"},
    )


def _forbid_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("SMTP must not open")

    monkeypatch.setattr(delivery.smtplib, "SMTP_SSL", forbidden)
    monkeypatch.setattr(delivery.smtplib, "SMTP", forbidden)


@pytest.mark.parametrize(
    "recipient",
    [
        "one@example.com,two@example.com",
        "owner@example.com\r\nBcc: hidden@example.com",
        "not-an-email",
    ],
)
def test_invalid_recipient_is_held_before_smtp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recipient: str) -> None:
    source = tmp_path / "brief.pdf"
    source.write_bytes(b"source")
    preview = delivery.prepare_messages(_config(), _report(), [_document(source, content=b"source")], tmp_path / "preview")[0]
    config = _config(send=True, recipient=recipient)
    monkeypatch.setenv("TEST_GMAIL_APP_PASSWORD", "app-password")
    _forbid_smtp(monkeypatch)

    result = delivery.deliver_messages(config, [preview], tmp_path / "state.sqlite")

    assert result[0]["status"] == "invalid_recipient"


def test_prepare_rejects_multiple_recipients_in_direct_config(tmp_path: Path) -> None:
    with pytest.raises(delivery.DeliveryError, match="exactly one explicit"):
        delivery.prepare_messages(
            _config(recipient="one@example.com,two@example.com"),
            _report(),
            [],
            tmp_path / "preview",
        )


def test_packaged_email_keeps_local_archive_paths_internal(tmp_path: Path) -> None:
    source = tmp_path / "private-archive" / "earnings.pdf"
    source.parent.mkdir()
    original = b"verified source copy"
    source.write_bytes(original)
    paths = delivery.prepare_messages(
        _config(), _report(), [_document(source, content=original)], tmp_path / "preview"
    )
    message = BytesParser(policy=policy.default).parsebytes(paths[0].read_bytes())
    for subtype in ("html", "plain"):
        body = message.get_body(preferencelist=(subtype,)).get_content()
        assert str(source) not in body
        assert "private-archive" not in body
        assert "https://investor.example/earnings.pdf" in body
    assert next(message.iter_attachments()).get_payload(decode=True) == original
    omission = {"label": "Additional report", "url": "https://investor.example/report.pdf",
                "local_path": str(source), "reason": "Exceeds message limit"}
    assert str(source) not in delivery._document_links_html([], [omission])


@pytest.mark.parametrize("recipient", [None, "", "   "])
def test_prepare_allows_unset_recipient_for_preview(tmp_path: Path, recipient: str | None) -> None:
    config = _config(recipient=recipient) if recipient is not None else _config(recipient=None)

    paths = delivery.prepare_messages(config, _report(), [], tmp_path / "preview")

    assert len(paths) == 1
    message = BytesParser(policy=policy.default).parsebytes(paths[0].read_bytes())
    assert message.get("To") is None


def test_live_send_without_recipient_is_held_before_smtp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preview = delivery.prepare_messages(_config(recipient=""), _report(), [], tmp_path / "preview")[0]
    config = _config(send=True, recipient="")
    monkeypatch.setenv("TEST_GMAIL_APP_PASSWORD", "app-password")
    _forbid_smtp(monkeypatch)

    result = delivery.deliver_messages(config, [preview], tmp_path / "state.sqlite")

    assert result[0]["status"] == "missing_recipient"


def test_invalid_smtp_security_is_held_without_opening_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preview = delivery.prepare_messages(_config(), _report(), [], tmp_path / "preview")[0]
    config = _config(send=True)
    config["email"]["smtp_security"] = "typo-tls-mode"
    monkeypatch.setenv("TEST_GMAIL_APP_PASSWORD", "app-password")
    _forbid_smtp(monkeypatch)

    result = delivery.deliver_messages(config, [preview], tmp_path / "state.sqlite")

    assert result[0]["status"] == "invalid_smtp_security"
    assert "unsupported SMTP security" in result[0]["detail"]


class _AcceptingSMTP:
    sent: list[object] = []

    def __init__(self, *args, **kwargs):
        pass

    def ehlo(self):
        return None

    def login(self, username, password):
        assert username == "sender@example.com"
        assert password == "app-password"

    def send_message(self, message, from_addr=None, to_addrs=None):
        self.__class__.sent.append(message)
        return {}

    def quit(self):
        return None


class _AmbiguousSMTP(_AcceptingSMTP):
    def send_message(self, message, from_addr=None, to_addrs=None):
        raise smtplib.SMTPServerDisconnected("connection dropped after DATA")


def test_changed_preview_cannot_reuse_ambiguous_message_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(send=True)
    preview = delivery.prepare_messages(config, _report(), [], tmp_path / "preview")[0]
    monkeypatch.setenv("TEST_GMAIL_APP_PASSWORD", "app-password")
    monkeypatch.setattr(delivery.smtplib, "SMTP_SSL", _AmbiguousSMTP)
    state = tmp_path / "state.sqlite"

    first = delivery.deliver_messages(config, [preview], state)
    preview.write_bytes(preview.read_bytes() + b"\n")
    assert delivery.reconcile_message(state, first[0]["message_key"], decision="retry", detail="provider checked")
    _AcceptingSMTP.sent = []
    monkeypatch.setattr(delivery.smtplib, "SMTP_SSL", _AcceptingSMTP)

    result = delivery.deliver_messages(config, [preview], state)

    assert result[0]["status"] == "message_changed_after_delivery_state"
    assert _AcceptingSMTP.sent == []


def test_reused_document_ids_cannot_collapse_distinct_parts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_data = b"a" * 3000
    second_data = b"b" * 3000
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    first.write_bytes(first_data)
    second.write_bytes(second_data)
    config = _config(limit=8200)
    documents = [
        _document(first, content=first_data, document_id="legacy-id"),
        _document(second, content=second_data, document_id="legacy-id"),
    ]

    paths = delivery.prepare_messages(config, _report(), documents, tmp_path / "preview")

    assert len(paths) == 2
    messages = [BytesParser(policy=policy.default).parsebytes(path.read_bytes()) for path in paths]
    assert messages[0]["X-Servicing-Brief-Message-Key"] != messages[1]["X-Servicing-Brief-Message-Key"]
    monkeypatch.setenv("TEST_GMAIL_APP_PASSWORD", "app-password")
    _AcceptingSMTP.sent = []
    monkeypatch.setattr(delivery.smtplib, "SMTP_SSL", _AcceptingSMTP)

    result = delivery.deliver_messages({**config, "_send": True}, paths, tmp_path / "state.sqlite")

    assert [row["status"] for row in result] == ["accepted", "accepted"]
    assert len(_AcceptingSMTP.sent) == 2


def test_html_bundle_rejects_duplicate_entry_names_and_normalizes_current_directory(
    tmp_path: Path,
) -> None:
    source = tmp_path / "exhibit.htm"
    image = tmp_path / "slide1.jpg"
    html = b'<html><img src="./slide1.jpg"></html>'
    image_bytes = b"slide bytes"
    source.write_bytes(html)
    image.write_bytes(image_bytes)
    metadata = {
        "document": "exhibit.htm",
        "assets": [
            {
                "document": "slide1.jpg",
                "path": str(image),
                "content_hash": hashlib.sha256(image_bytes).hexdigest().upper(),
            }
        ],
    }

    path, payload = html_bundle({"path": str(source), "metadata": metadata}, html)
    assert path.read_bytes() == payload
    with ZipFile(BytesIO(payload)) as archive:
        assert archive.read("slide1.jpg") == image_bytes

    duplicate = {**metadata, "assets": [metadata["assets"][0], dict(metadata["assets"][0])]}
    with pytest.raises(ValueError, match="duplicate"):
        html_bundle({"path": str(source), "metadata": duplicate}, html)


def _schedule_config(**overrides) -> dict:
    schedule = {
        "enabled": True,
        "timezone": "America/New_York",
        "times": ["07:00", "18:00"],
        "catch_up": True,
    }
    schedule.update(overrides)
    return {"schedule": schedule}


def test_schedule_requires_explicit_enablement(tmp_path: Path) -> None:
    config = {"schedule": {"timezone": "America/New_York", "times": ["07:00"]}}
    now = datetime(2026, 9, 4, 23, 30, tzinfo=timezone.utc)

    assert due(config, tmp_path / "state.sqlite", now=now) == []
    assert scheduled_run(config, tmp_path / "state.sqlite", lambda: {"status": "prepared"}, now=now)["status"] == "disabled"
    assert schedule_status(config, tmp_path / "state.sqlite")["enabled"] is False


@pytest.mark.parametrize("failure_key", ["errors", "preparation_errors", "delivery_errors"])
def test_schedule_keeps_slot_due_for_any_incomplete_result(tmp_path: Path, failure_key: str) -> None:
    state = tmp_path / "state.sqlite"
    now = datetime(2026, 9, 4, 23, 30, tzinfo=timezone.utc)
    result = scheduled_run(
        _schedule_config(),
        state,
        lambda: {"status": "prepared", failure_key: [{"reason": "incomplete"}]},
        now=now,
    )

    assert result["successful"] is False
    assert due(_schedule_config(), state, now=now)


def test_schedule_handles_dst_gap_and_first_fold(tmp_path: Path) -> None:
    config = _schedule_config(times=["02:30"], catch_up=True, catch_up_days=0)
    spring = datetime(2026, 3, 8, 12, 0, tzinfo=timezone.utc)
    assert due(config, tmp_path / "spring.sqlite", now=spring) == []

    fall_config = _schedule_config(times=["01:30"], catch_up=True)
    first_fold = datetime(2026, 11, 1, 6, 45, tzinfo=timezone.utc)
    slots = due(fall_config, tmp_path / "fall.sqlite", now=first_fold)
    assert len(slots) == 1
    assert slots[0]["scheduled_local"].endswith("-04:00")
