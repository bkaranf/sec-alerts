from __future__ import annotations

import os
import smtplib
from email import policy
from email.parser import BytesParser
from pathlib import Path

import pytest

from servicing_brief import delivery
from servicing_brief.models import Document


def _config(*, send: bool = False, limit: int | None = None, recipient: str | None = "owner@example.com") -> dict:
    email = {
        "from_address": "sender@example.com",
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 465,
        "smtp_security": "ssl",
        "smtp_username": "sender@example.com",
        "smtp_password_env": "TEST_GMAIL_APP_PASSWORD",
        "retry_attempts": 2,
    }
    if recipient is not None:
        email["recipient"] = recipient
    if limit is not None:
        email["max_message_bytes"] = limit
    config = {"email": email}
    if send:
        config["_send"] = True
    return config


def _report(*, body: str = "Readout", subject: str = "Brief") -> dict:
    """Build a normal fixture report with the production identity contract."""

    html = (
        "<html><body data-brief-company='TFC' data-brief-cik='0012345678' "
        "data-brief-event='2026-Q2'><p>" + body + "</p></body></html>"
    )
    text = f"Company: Truist Financial Corporation (TFC)\nEarnings period: Q2 2026\n\n{body}"
    return {
        "subject": subject,
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


def _document(path: Path, *, kind: str = "presentation", metadata: dict | None = None, expected_hash: str = "") -> Document:
    return Document(
        issuer="TFC",
        cik="0012345678",
        title="Quarterly presentation",
        kind=kind,
        source="ir",
        url="https://investor.example/source.pdf",
        published="2026-08-01T12:00:00+00:00",
        period="2026-Q2",
        path=str(path),
        content_hash=expected_hash,
        metadata={"ticker": "TFC", **(metadata or {})},
    )


def test_preview_omits_unset_recipient_and_keeps_original_bytes(tmp_path: Path) -> None:
    original = b"original issuer bytes\x00\x01"
    source = tmp_path / "presentation.pdf"
    source.write_bytes(original)
    config = _config(recipient=None)
    report = _report()

    paths = delivery.prepare_messages(config, report, [_document(source)], tmp_path / "preview")

    assert len(paths) == 1
    message = BytesParser(policy=policy.default).parsebytes(paths[0].read_bytes())
    assert message.get("To") is None
    attachments = [part for part in message.walk() if part.get_content_disposition() == "attachment"]
    assert len(attachments) == 1
    assert attachments[0].get_payload(decode=True) == original
    assert paths[0].read_bytes() == paths[0].read_bytes()


def test_generated_print_copy_requires_verification(tmp_path: Path) -> None:
    source = tmp_path / "print.pdf"
    source.write_bytes(b"generated print")
    document = _document(source, metadata={"generated_print_copy": True})

    path = delivery.prepare_messages(_config(), _report(body="x"), [document], tmp_path / "preview")[0]
    message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())

    assert not [part for part in message.walk() if part.get_content_disposition() == "attachment"]
    assert "unverified generated print copy" in path.read_text(encoding="utf-8", errors="replace")


def test_fully_encoded_size_splits_and_omits_single_oversized_document(tmp_path: Path) -> None:
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    first.write_bytes(b"a" * 3000)
    second.write_bytes(b"b" * 3000)
    report = _report()

    paths = delivery.prepare_messages(
        _config(limit=8200),
        report,
        [_document(first), _document(second)],
        tmp_path / "preview",
    )

    assert len(paths) == 2
    assert all(path.stat().st_size <= 8200 for path in paths)
    payloads = []
    for path in paths:
        message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
        payloads.extend(part.get_payload(decode=True) for part in message.walk() if part.get_content_disposition() == "attachment")
    assert payloads == [b"a" * 3000, b"b" * 3000]

    too_large = tmp_path / "too-large.pdf"
    too_large.write_bytes(b"z" * 4000)
    omitted_path = delivery.prepare_messages(
        _config(limit=4300),
        report,
        [_document(too_large)],
        tmp_path / "omitted",
    )[0]
    assert omitted_path.stat().st_size <= 4300
    omitted_message = BytesParser(policy=policy.default).parsebytes(omitted_path.read_bytes())
    omitted_text = omitted_message.get_body(preferencelist=("plain",)).get_content()
    assert "attachment exceeds configured fully-encoded message limit" in omitted_text
    assert 'original copy retained locally' in omitted_text
    assert str(too_large) not in omitted_text
    # Local provenance must not leak through reader-facing HTML tooltips.
    assert str(too_large) not in omitted_message.get_body(preferencelist=('html',)).get_content()


def test_package_key_changes_when_explicit_recipient_changes(tmp_path: Path) -> None:
    report = _report(body="x")
    first = delivery.prepare_messages(_config(recipient="one@example.com"), report, [], tmp_path / "one")[0]
    second = delivery.prepare_messages(_config(recipient="two@example.com"), report, [], tmp_path / "two")[0]
    one = BytesParser(policy=policy.default).parsebytes(first.read_bytes())
    two = BytesParser(policy=policy.default).parsebytes(second.read_bytes())
    assert one["X-Servicing-Brief-Message-Key"] != two["X-Servicing-Brief-Message-Key"]


class _AcceptingSMTP:
    sent: list[object] = []

    def __init__(self, *args, **kwargs):
        self.args = args

    def ehlo(self):
        return None

    def login(self, username, password):
        assert username == "sender@example.com"
        assert password == "app-password"

    def starttls(self, context=None):
        return None

    def send_message(self, message, from_addr=None, to_addrs=None):
        self.__class__.sent.append(message)
        return {}

    def quit(self):
        return None


def test_provider_acceptance_is_deduplicated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_GMAIL_APP_PASSWORD", "app-password")
    _AcceptingSMTP.sent = []
    monkeypatch.setattr(delivery.smtplib, "SMTP_SSL", _AcceptingSMTP)
    config = _config(send=True)
    report = _report(body="x")
    path = delivery.prepare_messages(config, report, [], tmp_path / "preview")[0]
    state = tmp_path / "state.sqlite"

    first = delivery.deliver_messages(config, [path], state)
    second = delivery.deliver_messages(config, [path], state)

    assert first[0]["status"] == "accepted"
    assert second[0]["status"] == "already_accepted"
    assert len(_AcceptingSMTP.sent) == 1
    row = next(iter(delivery.delivery_status(state)["recent"]))
    assert row["status"] == "accepted"


def test_gmail_starttls_port_is_supported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_GMAIL_APP_PASSWORD", "app-password")
    _AcceptingSMTP.sent = []
    monkeypatch.setattr(delivery.smtplib, "SMTP", _AcceptingSMTP)
    config = _config(send=True)
    config["email"].update({"smtp_port": 587, "smtp_security": "starttls"})
    report = _report(body="x")
    path = delivery.prepare_messages(config, report, [], tmp_path / "preview")[0]

    result = delivery.deliver_messages(config, [path], tmp_path / "state.sqlite")

    assert result[0]["status"] == "accepted"


class _AmbiguousSMTP(_AcceptingSMTP):
    sent_calls = 0

    def send_message(self, message, from_addr=None, to_addrs=None):
        self.__class__.sent_calls += 1
        raise smtplib.SMTPServerDisconnected("connection dropped after DATA")


def test_possible_post_data_acceptance_is_not_blindly_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_GMAIL_APP_PASSWORD", "app-password")
    _AmbiguousSMTP.sent_calls = 0
    monkeypatch.setattr(delivery.smtplib, "SMTP_SSL", _AmbiguousSMTP)
    config = _config(send=True)
    report = _report(body="x")
    path = delivery.prepare_messages(config, report, [], tmp_path / "preview")[0]
    state = tmp_path / "state.sqlite"

    first = delivery.deliver_messages(config, [path], state)
    second = delivery.deliver_messages(config, [path], state)

    assert first[0]["status"] == "ambiguous"
    assert second[0]["status"] == "ambiguous"
    assert _AmbiguousSMTP.sent_calls == 1


def test_ambiguous_delivery_requires_explicit_reconciliation_before_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_GMAIL_APP_PASSWORD", "app-password")
    _AmbiguousSMTP.sent_calls = 0
    monkeypatch.setattr(delivery.smtplib, "SMTP_SSL", _AmbiguousSMTP)
    config = _config(send=True)
    report = _report(body="x")
    path = delivery.prepare_messages(config, report, [], tmp_path / "preview")[0]
    state = tmp_path / "state.sqlite"

    first = delivery.deliver_messages(config, [path], state)
    key = first[0]["message_key"]
    assert delivery.reconcile_message(state, key, decision="retry", detail="Gmail provider checked; no acceptance")['status'] == "retry_ready"
    second = delivery.deliver_messages(config, [path], state)

    assert second[0]["status"] == "ambiguous"
    assert _AmbiguousSMTP.sent_calls == 2


def test_pre_send_oversize_is_refused_without_opening_smtp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_GMAIL_APP_PASSWORD", "app-password")
    opened = []

    class NeverSMTP(_AcceptingSMTP):
        def __init__(self, *args, **kwargs):
            opened.append(True)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(delivery.smtplib, "SMTP_SSL", NeverSMTP)
    message = delivery._make_message(
        _config(send=True, limit=100),
        _report(body="X" * 5000),
        [],
        [],
        subject="Brief",
        package_key="package-key",
        message_key="message-key",
        part_number=1,
        total_parts=1,
    )
    raw = tmp_path / "too-large.eml"
    raw.write_bytes(message.as_bytes(policy=policy.SMTP))
    result = delivery.deliver_messages(_config(send=True, limit=100), [raw], tmp_path / "state.sqlite")

    assert result[0]["status"] == "oversize_refused"
    assert opened == []


def test_missing_credentials_returns_local_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_GMAIL_APP_PASSWORD", raising=False)
    config = _config(send=True)
    report = _report(body="x")
    path = delivery.prepare_messages(config, report, [], tmp_path / "preview")[0]

    result = delivery.deliver_messages(config, [path], tmp_path / "state.sqlite")

    assert result[0]["status"] == "missing_credentials"
    assert "TEST_GMAIL_APP_PASSWORD" in result[0]["detail"]
