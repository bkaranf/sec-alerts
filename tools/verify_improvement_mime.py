"""Build and verify held, offline MIME previews for the loop-1 redesign.

This is a proof tool for the two current review bodies.  It reads the already
rendered HTML/text files, invokes the delivery logo helper, and writes local
draft artifacts.  It never opens SMTP or performs network I/O.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output" / "brief-improvement"
LOOP_OUTPUT = OUTPUT / (sys.argv[1] if len(sys.argv) > 1 else "loop-1")
BRANDING_REGISTRY = ROOT / "servicing_brief" / "company_branding.json"
FROM_TO = "bkaranf5@gmail.com"
HOLD_HEADER = "X-Servicing-Brief-Local-Hold"
HOLD_VALUE = "true"
GENERATED_AT = datetime(2026, 9, 5, 16, 0, tzinfo=timezone.utc)

sys.path.insert(0, str(ROOT))
from servicing_brief.branding import lookup_brand  # noqa: E402
from servicing_brief.delivery import embed_brand_logos  # noqa: E402


_CID_SRC = re.compile(
    r"(?P<prefix>\bsrc\s*=\s*)(?P<quote>[\"'])(?P<value>cid:[^\"']+)(?P=quote)",
    re.IGNORECASE,
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _capture_proof(path: Path, state: str) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"missing required visual capture {path}")
    data = path.read_bytes()
    return {
        "path": str(path.relative_to(ROOT)).replace("\\", "/"),
        "state": state,
        "bytes": len(data),
        "sha256": _sha256(data),
    }


def _visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return " ".join(soup.get_text(" ", strip=True).split())


def _brand_attributes(html: str) -> dict[str, dict[str, str | None]]:
    soup = BeautifulSoup(html, "html.parser")
    result: dict[str, dict[str, str | None]] = {}
    for image in soup.find_all("img"):
        ticker = image.get("data-brand-ticker")
        if not ticker:
            continue
        result[str(ticker).upper()] = {
            "alt": image.get("alt"),
            "width": image.get("width"),
            "height": image.get("height"),
            "src": image.get("src"),
        }
    return result


def _asset_for(ticker: str) -> tuple[dict[str, Any], bytes, str, str]:
    brand = lookup_brand(ticker)
    if brand.get("verified") is not True:
        raise ValueError(f"{ticker}: brand registry record is not verified")
    path = Path(str(brand["local_asset_path"])).resolve(strict=True)
    data = path.read_bytes()
    digest = _sha256(data)
    registry = json.loads(BRANDING_REGISTRY.read_text(encoding="utf-8"))
    declared = registry.get("brands", {}).get(ticker, {}).get("asset_sha256")
    if not isinstance(declared, str) or declared.casefold() != digest.casefold():
        raise ValueError(f"{ticker}: local PNG does not match the registry asset_sha256")
    cid = f"brand-{ticker.lower()}-{digest[:24]}@servicing-brief.local"
    return brand, data, cid, declared


def _expected_cids(source_html: str) -> tuple[dict[str, dict[str, Any]], str]:
    brands: dict[str, dict[str, Any]] = {}
    expected_html = source_html
    for ticker, attrs in _brand_attributes(source_html).items():
        brand, data, cid, declared_sha = _asset_for(ticker)
        source = attrs.get("src")
        if not isinstance(source, str) or not source:
            raise ValueError(f"{ticker}: rendered HTML has no logo source")
        expected_html = expected_html.replace(source, f"cid:{cid}", 1)
        brands[ticker] = {
            "brand": brand,
            "data": data,
            "cid": cid,
            "source": source,
            "source_sha256": _sha256(data),
            "registry_sha256": declared_sha,
            "alt": attrs.get("alt"),
            "width": attrs.get("width"),
            "height": attrs.get("height"),
        }
    if not brands:
        raise ValueError("rendered HTML contains no data-brand-ticker logo")
    return brands, expected_html


def _build_message(label: str, html: str, text: str, subject: str) -> tuple[EmailMessage, str, str]:
    key = hashlib.sha256((label + "\x00" + html + "\x00" + text).encode("utf-8")).hexdigest()[:32]
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = FROM_TO
    message["To"] = FROM_TO
    message["Subject"] = subject
    message["Date"] = format_datetime(GENERATED_AT, usegmt=True)
    message["Message-ID"] = f"<{key}@servicing-brief.local>"
    message["X-Servicing-Brief-Message-Key"] = key
    message[HOLD_HEADER] = HOLD_VALUE
    message["X-Servicing-Brief-Status"] = "DRAFT_REVIEW_HOLD"
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    rewritten = embed_brand_logos(message, html)
    # There are no document attachments in this isolated proof.  Set the
    # top-level alternative boundary explicitly to match delivery.py's stable
    # boundary convention; the helper sets the nested related boundary.
    message.set_boundary(f"=_servicing_brief_{key[:24]}")
    return message, key, rewritten


def _decoded_html(parsed: EmailMessage) -> str:
    html_part = parsed.get_body(preferencelist=("html",))
    if html_part is None:
        raise ValueError("MIME preview has no HTML alternative")
    html = html_part.get_content()
    cid_data: dict[str, bytes] = {}
    for part in parsed.walk():
        if part.get_content_type() != "image/png":
            continue
        raw_cid = part.get("Content-ID")
        if not raw_cid:
            raise ValueError("MIME image is missing Content-ID")
        cid_data[raw_cid.strip().strip("<>")] = part.get_payload(decode=True) or b""

    def replace(match: re.Match[str]) -> str:
        value = match.group("value")
        cid = value.removeprefix("cid:")
        data = cid_data.get(cid)
        if data is None:
            raise ValueError(f"HTML references missing MIME Content-ID {cid}")
        quote = match.group("quote")
        return f"{match.group('prefix')}{quote}data:image/png;base64,{base64.b64encode(data).decode('ascii')}{quote}"

    decoded = _CID_SRC.sub(replace, html)
    if "cid:" in decoded:
        raise ValueError("decoded preview still contains a CID reference")
    return decoded


def _verify_one(label: str, html_path: Path, text_path: Path, eml_name: str, decoded_name: str, subject: str) -> dict[str, Any]:
    source_html_bytes = html_path.read_bytes()
    source_text_bytes = text_path.read_bytes()
    source_html = source_html_bytes.decode("utf-8")
    source_text = source_text_bytes.decode("utf-8")
    brands, expected_html = _expected_cids(source_html)
    first, key, rewritten = _build_message(label, source_html, source_text, subject)
    second, second_key, second_rewritten = _build_message(label, source_html, source_text, subject)
    first_bytes = first.as_bytes(policy=policy.SMTP)
    second_bytes = second.as_bytes(policy=policy.SMTP)
    if first_bytes != second_bytes or key != second_key or rewritten != second_rewritten:
        raise ValueError(f"{label}: repeated MIME build is not byte stable")
    if rewritten != expected_html:
        raise ValueError(f"{label}: HTML changed beyond the branded src values")

    eml_path = LOOP_OUTPUT / eml_name
    eml_path.write_bytes(first_bytes)
    parsed = BytesParser(policy=policy.default).parsebytes(first_bytes)
    parsed_html_part = parsed.get_body(preferencelist=("html",))
    parsed_plain_part = parsed.get_body(preferencelist=("plain",))
    if parsed_html_part is None or parsed_plain_part is None:
        raise ValueError(f"{label}: parsed MIME is missing plain or HTML body")
    parsed_html = parsed_html_part.get_content()
    parsed_text = parsed_plain_part.get_content()
    plain_text_unchanged = parsed_text.replace("\r\n", "\n") == source_text.replace("\r\n", "\n")
    if not plain_text_unchanged:
        raise ValueError(f"{label}: plain text changed after MIME encoding")
    if _visible_text(parsed_html) != _visible_text(source_html):
        raise ValueError(f"{label}: visible HTML text changed after CID replacement")
    if "\u2014" in source_text or "\u2014" in source_html or "\u2014" in parsed_text or "\u2014" in parsed_html:
        raise ValueError(f"{label}: em dash found in held review")
    if len(parsed_html.encode("utf-8")) >= 90 * 1024:
        raise ValueError(f"{label}: post-CID HTML exceeds 90KB")

    parsed_attrs = _brand_attributes(parsed_html)
    images = [part for part in parsed.walk() if part.get_content_type() == "image/png"]
    if len(images) != len(brands):
        raise ValueError(f"{label}: expected {len(brands)} PNG related parts, got {len(images)}")
    image_by_cid = {
        str(part["Content-ID"]).strip().strip("<>"): part
        for part in images
        if part.get("Content-ID")
    }
    image_proof: list[dict[str, Any]] = []
    for ticker, detail in brands.items():
        cid = detail["cid"]
        part = image_by_cid.get(cid)
        if part is None:
            raise ValueError(f"{label}: missing expected Content-ID {cid}")
        embedded = part.get_payload(decode=True) or b""
        if embedded != detail["data"]:
            raise ValueError(f"{label}: embedded PNG bytes differ for {ticker}")
        attrs = parsed_attrs.get(ticker)
        if attrs is None:
            raise ValueError(f"{label}: parsed HTML lost {ticker} brand tag")
        if any(attrs.get(name) != detail[name] for name in ("alt", "width", "height")):
            raise ValueError(f"{label}: alt or dimensions changed for {ticker}")
        image_proof.append(
            {
                "ticker": ticker,
                "content_id": cid,
                "embedded_sha256": _sha256(embedded),
                "local_png_sha256": detail["source_sha256"],
                "registry_asset_sha256": detail["registry_sha256"],
                "sha256_match": _sha256(embedded) == detail["source_sha256"],
                "bytes": len(embedded),
                "alt": attrs["alt"],
                "width": attrs["width"],
                "height": attrs["height"],
            }
        )
    if set(image_by_cid) != {detail["cid"] for detail in brands.values()}:
        raise ValueError(f"{label}: MIME contains an unexpected brand Content-ID")

    decoded_path = LOOP_OUTPUT / decoded_name
    decoded = _decoded_html(parsed)
    decoded_path.write_bytes(decoded.encode("utf-8"))
    if len(_brand_attributes(decoded)) != len(brands):
        raise ValueError(f"{label}: decoded preview lost brand tags")

    related_parts = [part for part in parsed.walk() if part.is_multipart() and part.get_content_type() == "multipart/related"]
    if len(related_parts) != 1:
        raise ValueError(f"{label}: expected one multipart/related HTML container")
    return {
        "label": label,
        "subject": subject,
        "from": FROM_TO,
        "to": FROM_TO,
        "local_hold_header": {HOLD_HEADER: parsed.get(HOLD_HEADER), "X-Servicing-Brief-Status": parsed.get("X-Servicing-Brief-Status")},
        "message_key": key,
        "eml": str(eml_path.relative_to(ROOT)).replace("\\", "/"),
        "decoded_html": str(decoded_path.relative_to(ROOT)).replace("\\", "/"),
        "eml_sha256": _sha256(first_bytes),
        "eml_bytes": len(first_bytes),
        "repeat_eml_bytes_stable": True,
        "source_html": str(html_path.relative_to(ROOT)).replace("\\", "/"),
        "source_text": str(text_path.relative_to(ROOT)).replace("\\", "/"),
        "source_html_sha256": _sha256(source_html_bytes),
        "source_text_sha256": _sha256(source_text_bytes),
        "source_html_content_sha256": _sha256(source_html.encode("utf-8")),
        "source_text_content_sha256": _sha256(source_text.encode("utf-8")),
        "post_cid_html_bytes": len(parsed_html.encode("utf-8")),
        "html_under_90kb": len(parsed_html.encode("utf-8")) < 90 * 1024,
        "visible_html_text_unchanged": _visible_text(parsed_html) == _visible_text(source_html),
        "plain_text_unchanged": plain_text_unchanged,
        "html_src_only_rewrite": rewritten == expected_html,
        "no_em_dash": "\u2014" not in source_html and "\u2014" not in source_text,
        "related_boundary": related_parts[0].get_boundary(),
        "top_boundary": parsed.get_boundary(),
        "image_count": len(images),
        "all_cids_match_local_png": all(item["sha256_match"] for item in image_proof),
        "image_alt_dimensions_preserved": True,
        "images": image_proof,
    }


def main() -> int:
    LOOP_OUTPUT.mkdir(parents=True, exist_ok=True)
    messages = [
        _verify_one(
            "combined",
            OUTPUT / "gmail-body.html",
            OUTPUT / "combined-email.txt",
            "combined-preview.eml",
            "combined-preview-decoded.html",
            f"DRAFT REVIEW: Combined mortgage servicing brief ({LOOP_OUTPUT.name} MIME QA)",
        ),
        _verify_one(
            "PFSI",
            OUTPUT / "PFSI-review.html",
            OUTPUT / "PFSI-review.txt",
            "PFSI-preview.eml",
            "PFSI-preview-decoded.html",
            f"DRAFT REVIEW: PFSI servicing earnings brief ({LOOP_OUTPUT.name} MIME QA)",
        ),
    ]
    proof = {
        "version": 1,
        "status": "PASS_LOCAL_HOLD",
        "purpose": "Offline MIME/CID proof for held QA drafts; no SMTP send was attempted.",
        "generated_at": GENERATED_AT.isoformat(),
        "from_to": FROM_TO,
        "smtp_attempted": False,
        "network_used": False,
        "local_png_registry": "servicing_brief/company_branding.json",
        "visual_captures": [
            _capture_proof(LOOP_OUTPUT / "qa" / "PFSI-inline-390.png", "logos-on"),
            _capture_proof(LOOP_OUTPUT / "qa" / "PFSI-inline-images-blocked-390.png", "logos-blocked"),
        ],
        "messages": messages,
        "limits": [
            "The artifacts are local DRAFT REVIEW holds only; provider acceptance was not tested.",
            "The proof covers the current combined and PFSI rendered bodies and does not approve future regenerated content.",
        ],
    }
    (LOOP_OUTPUT / "mime-proof.json").write_text(
        json.dumps(proof, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(proof, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
