"""Official issuer investor-relations discovery and document collection."""

from __future__ import annotations

import contextlib
import email.utils
import ipaddress
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from servicing_brief.models import SourceResult

from .common import (
    archive_bytes,
    clean_part,
    company_name,
    company_ticker,
    extension_for,
    infer_content_period,
    infer_period,
    latest_completed_period,
    looks_like_document,
    normalize_cik,
    source_document,
    sha256_bytes,
    _safe_error,
    _sources_config,
    _storage,
    _period_order,
    title_kind,
    utc_now,
    write_json_atomic,
)


_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
_BLOCKED_STATUS = {401, 403}
_MAX_RETRY_AFTER = 30.0
_MAX_REDIRECTS = 5
_REDIRECT_STATUS = {301, 302, 303, 307, 308}
_PRIVATE_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "0.0.0.0",
}


class IRSourceError(RuntimeError):
    """A bounded IR request failure with safe status metadata."""

    def __init__(self, message: str, *, blocked: bool = False, status_code: int | None = None):
        super().__init__(message)
        self.blocked = blocked
        self.status_code = status_code


class RobotsBlocked(IRSourceError):
    def __init__(self, url: str):
        super().__init__(f"robots.txt disallows configured user agent for {url}", blocked=True, status_code=403)


@dataclass
class _Fetched:
    url: str
    final_url: str
    status_code: int
    content: bytes
    headers: dict[str, str]


def _user_agent(config: dict[str, Any]) -> str:
    configured = _sources_config(config).get("ir_user_agent") or config.get("ir_user_agent")
    return str(configured or "mortgage-servicing-brief/0.1 (+official-source-collection)")[:200]


def _request_timeout(config: dict[str, Any]) -> float:
    value = _sources_config(config).get("ir_timeout", 20.0)
    try:
        return max(2.0, min(float(value), 60.0))
    except (TypeError, ValueError):
        return 20.0


def _max_retries(config: dict[str, Any]) -> int:
    value = _sources_config(config).get("ir_max_retries", 2)
    try:
        return max(0, min(int(value), 3))
    except (TypeError, ValueError):
        return 2


def _max_pages(config: dict[str, Any]) -> int:
    value = _sources_config(config).get("max_ir_pages", 8)
    try:
        return max(1, min(int(value), 20))
    except (TypeError, ValueError):
        return 8


def _max_documents(config: dict[str, Any]) -> int:
    value = _sources_config(config).get("max_ir_documents", 16)
    try:
        return max(1, min(int(value), 40))
    except (TypeError, ValueError):
        return 16


def _max_document_bytes(config: dict[str, Any]) -> int:
    value = _sources_config(config).get("max_ir_document_bytes", 30 * 1024 * 1024)
    try:
        return max(1024, min(int(value), 100 * 1024 * 1024))
    except (TypeError, ValueError):
        return 30 * 1024 * 1024


def _manifest_path(config: dict[str, Any]) -> Path:
    return _storage(config) / "archive" / "source-manifest.json"


def _load_manifest(config: dict[str, Any]) -> dict[str, Any]:
    path = _manifest_path(config)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return {}


def _cache_path(config: dict[str, Any], url: str) -> Path:
    import hashlib

    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    path = _storage(config) / "archive" / ".cache" / f"{digest}.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _safe_http_url(url: str) -> tuple[bool, str]:
    """Validate an HTTP URL before it can reach the transport layer.

    IR document links may legitimately move to a public issuer CDN, so this
    check does not require the target host to match the configured IR host.
    It does reject credentials and local/reserved destinations, which would
    otherwise make an official-page redirect an SSRF primitive.

    Hostnames are intentionally not DNS-resolved here.  Private-address DNS
    answers and rebinding remain a deployment/network boundary; literal
    private addresses and ambiguous numeric host forms are rejected locally.
    """

    try:
        parsed = urlparse(str(url or ""))
        host = (parsed.hostname or "").rstrip(".").lower()
    except ValueError:
        return False, "malformed URL"
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        return False, "URL is not HTTP(S)"
    if parsed.username is not None or parsed.password is not None:
        return False, "URL credentials are not allowed"
    # URL parsers and ``ipaddress`` intentionally accept only canonical IP
    # spellings.  Browsers and some HTTP stacks also interpret shorthand or
    # integer IPv4 forms, which can disguise loopback/private destinations.
    dotted_numeric = bool(re.fullmatch(r"[0-9.]+", host))
    if (dotted_numeric and host.count(".") != 3) or re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)", host):
        return False, "ambiguous numeric hostname is not allowed"
    if host in _PRIVATE_HOSTNAMES or host.endswith(".localhost") or host.endswith(".local"):
        return False, "local hostname is not allowed"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if dotted_numeric:
            return False, "ambiguous numeric hostname is not allowed"
        address = None
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        return False, "private or reserved address is not allowed"
    return True, ""


def _header(headers: Any, name: str) -> str:
    """Read a response header from case-sensitive test doubles or HTTPX."""

    wanted = name.lower()
    try:
        for key, value in headers.items():
            if str(key).lower() == wanted:
                return str(value or "")
    except Exception:
        return ""
    return ""


def _safe_cache_path(config: dict[str, Any], value: Any) -> Path | None:
    """Accept only cache files beneath this source's cache directory."""

    if not value:
        return None
    root = (_storage(config) / "archive" / ".cache").resolve()
    try:
        candidate = Path(str(value)).expanduser().resolve()
        candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate


def _write_cache(path: Path, content: bytes) -> None:
    # Small cache writes can use the common atomic JSON helper only for JSON;
    # use a temporary sibling and replace here to preserve binary bytes.
    import tempfile

    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _robots_entry(
    config: dict[str, Any],
    session: httpx.Client,
    robots_cache: dict[str, tuple[RobotFileParser | None, int | None]],
    url: str,
) -> tuple[RobotFileParser | None, int | None]:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return None, None
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin in robots_cache:
        return robots_cache[origin]
    robots_url = f"{origin}/robots.txt"
    safe, _ = _safe_http_url(robots_url)
    if not safe:
        robots_cache[origin] = (None, 403)
        return robots_cache[origin]
    try:
        # Follow only a short, explicitly validated robots redirect chain.  A
        # malicious or compromised issuer page must not turn this probe into
        # an unvalidated request to a private host, while ordinary www to
        # canonical-host redirects remain usable.
        for _ in range(3):
            response = session.get(robots_url, headers={"User-Agent": _user_agent(config)}, timeout=_request_timeout(config), follow_redirects=False)
            redirect_status = int(getattr(response, "status_code", 0) or 0)
            if redirect_status not in _REDIRECT_STATUS:
                break
            location = _header(getattr(response, "headers", {}), "location")
            target, _ = urldefrag(urljoin(robots_url, location)) if location else ("", "")
            safe, _ = _safe_http_url(target)
            if not safe:
                robots_cache[origin] = (None, 403)
                return robots_cache[origin]
            robots_url = target
        else:
            robots_cache[origin] = (None, 403)
            return robots_cache[origin]
    except Exception:
        # Unknown robots policy is recorded as unavailable by the caller; do
        # not pretend a fetch was blocked or bypass the site with retries.
        robots_cache[origin] = (None, None)
        return robots_cache[origin]
    status = int(getattr(response, "status_code", 0) or 0)
    if status in _BLOCKED_STATUS:
        robots_cache[origin] = (None, status)
        return robots_cache[origin]
    if status == 404:
        robots_cache[origin] = (None, status)
        return robots_cache[origin]
    if status != 200:
        robots_cache[origin] = (None, status)
        return robots_cache[origin]
    parser = RobotFileParser()
    try:
        parser.set_url(robots_url)
        parser.parse(response.text.splitlines())
    except Exception:
        parser = None
    robots_cache[origin] = (parser, status)
    return robots_cache[origin]


def _robots_allowed(
    config: dict[str, Any],
    session: httpx.Client,
    robots_cache: dict[str, tuple[RobotFileParser | None, int | None]],
    url: str,
) -> tuple[bool, str]:
    parser, status = _robots_entry(config, session, robots_cache, url)
    if status in _BLOCKED_STATUS:
        return False, "robots.txt returned an access-block status"
    if parser is None:
        # A missing/unreadable robots file is an explicit limitation, but a
        # 404 is the standard signal that no policy is published.  Continue in
        # that case; for transient failures continue once and record no bypass.
        return True, "robots policy unavailable or not published"
    try:
        allowed = parser.can_fetch(_user_agent(config), url)
    except Exception:
        allowed = True
    return bool(allowed), "robots.txt disallow" if not allowed else "robots.txt allow"


def _retry_after(headers: dict[str, Any]) -> float:
    raw = str(headers.get("retry-after", headers.get("Retry-After", "")) or "").strip()
    if not raw:
        return 0.0
    try:
        return max(0.0, min(float(raw), _MAX_RETRY_AFTER))
    except ValueError:
        with contextlib.suppress(Exception):
            when = email.utils.parsedate_to_datetime(raw)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0.0, min((when - datetime.now(timezone.utc)).total_seconds(), _MAX_RETRY_AFTER))
    return 0.0


def _fetch(
    config: dict[str, Any],
    session: httpx.Client,
    robots_cache: dict[str, tuple[RobotFileParser | None, int | None]],
    manifest: dict[str, Any],
    url: str,
) -> _Fetched:
    """Fetch one official IR URL with robots, cache, and bounded retries."""

    canonical, _ = urldefrag(url)
    safe, _ = _safe_http_url(canonical)
    if not safe:
        raise IRSourceError("issuer IR URL is not a safe public HTTP URL", blocked=True, status_code=400)
    prior = manifest.get(canonical, {}) if isinstance(manifest.get(canonical), dict) else {}
    headers = {"User-Agent": _user_agent(config), "Accept": "text/html,application/pdf,application/*,text/plain;q=0.8,*/*;q=0.1"}
    if prior.get("etag"):
        headers["If-None-Match"] = str(prior["etag"])
    if prior.get("last_modified"):
        headers["If-Modified-Since"] = str(prior["last_modified"])
    retries = _max_retries(config)
    for attempt in range(retries + 1):
        current_url = canonical
        redirect_count = 0
        request_headers = dict(headers)
        response = None
        status = 0
        response_headers: dict[str, str] = {}
        while True:
            allowed, _ = _robots_allowed(config, session, robots_cache, current_url)
            if not allowed:
                raise RobotsBlocked(current_url)
            try:
                response = session.get(current_url, headers=request_headers, timeout=_request_timeout(config), follow_redirects=False)
            except (httpx.RequestError, OSError) as exc:
                response = None
                if attempt >= retries:
                    raise IRSourceError(f"request failed for {canonical}: {_safe_error(exc)}") from exc
                time.sleep(min(2.0 ** attempt, 8.0))
                break
            status = int(getattr(response, "status_code", 0) or 0)
            response_headers = {str(k): str(v) for k, v in getattr(response, "headers", {}).items()}
            if status not in _REDIRECT_STATUS:
                break
            if redirect_count >= _MAX_REDIRECTS:
                raise IRSourceError("issuer IR redirect chain exceeded the configured limit", status_code=status)
            location = _header(getattr(response, "headers", {}), "location")
            if not location:
                raise IRSourceError("issuer IR redirect did not provide a location", status_code=status)
            target, _ = urldefrag(urljoin(current_url, location))
            safe, _ = _safe_http_url(target)
            if not safe:
                raise IRSourceError("issuer IR redirect target is not a safe public HTTP URL", blocked=True, status_code=status)
            # Validators are for the requested URL.  Do not send an issuer's
            # ETag or Last-Modified token to an unrelated CDN target.
            request_headers.pop("If-None-Match", None)
            request_headers.pop("If-Modified-Since", None)
            current_url = target
            redirect_count += 1
        # A transport exception that is retryable has already slept and
        # broken out of the redirect loop.  Start the next bounded attempt.
        if response is None:
            continue
        if status == 304:
            cached = prior.get("cache_path")
            cached_path = _safe_cache_path(config, cached)
            if cached_path and cached_path.exists():
                try:
                    cached_content = cached_path.read_bytes()
                    expected_hash = str(prior.get("content_hash") or "").strip().lower()
                    within_limit = len(cached_content) <= _max_document_bytes(config)
                    valid_hash = bool(re.fullmatch(r"[0-9a-f]{64}", expected_hash))
                    intact = valid_hash and sha256_bytes(cached_content) == expected_hash
                    if within_limit and intact:
                        final_url = str(getattr(response, "url", current_url) or current_url)
                        safe, _ = _safe_http_url(final_url)
                        if not safe:
                            raise IRSourceError("issuer IR response URL is not a safe public HTTP URL", blocked=True, status_code=304)
                        return _Fetched(canonical, final_url, 304, cached_content, response_headers)
                except OSError:
                    pass
            # A stale conditional cache entry cannot produce a document; make
            # one unconditional attempt, still within the bounded retry cap.
            request_headers.pop("If-None-Match", None)
            request_headers.pop("If-Modified-Since", None)
            headers.pop("If-None-Match", None)
            headers.pop("If-Modified-Since", None)
            if attempt < retries:
                continue
            raise IRSourceError(f"server returned 304 but no cached copy exists for {canonical}", status_code=304)
        if status in _BLOCKED_STATUS:
            raise IRSourceError(f"issuer IR access blocked ({status}) for {canonical}", blocked=True, status_code=status)
        if status == 404:
            raise IRSourceError(f"issuer IR document not found (404) for {canonical}", status_code=status)
        if status in _RETRYABLE_STATUS and attempt < retries:
            delay = _retry_after(response_headers) or min(2.0 ** attempt, 8.0)
            time.sleep(delay)
            continue
        if status >= 400:
            raise IRSourceError(f"issuer IR request returned HTTP {status} for {canonical}", status_code=status)
        content = bytes(getattr(response, "content", b"") or b"")
        if len(content) > _max_document_bytes(config):
            raise IRSourceError(f"issuer IR response exceeds configured size limit for {canonical}", status_code=status)
        final_url = str(getattr(response, "url", current_url) or current_url)
        safe, _ = _safe_http_url(final_url)
        if not safe:
            raise IRSourceError("issuer IR response URL is not a safe public HTTP URL", blocked=True, status_code=status)
        cache = _cache_path(config, canonical)
        _write_cache(cache, content)
        manifest[canonical] = {
            "etag": _header(response_headers, "etag"),
            "last_modified": _header(response_headers, "last-modified"),
            "cache_path": str(cache),
            "retrieved": utc_now(),
            "status_code": status,
            "final_url": final_url,
            "content_type": _header(response_headers, "content-type"),
            "content_hash": sha256_bytes(content),
        }
        return _Fetched(canonical, final_url, status, content, response_headers)
    raise IRSourceError(f"issuer IR request exhausted retries for {canonical}")


def _urls(company: dict[str, Any]) -> list[str]:
    values: list[Any] = []
    pages = company.get("ir_pages", [])
    if isinstance(pages, str):
        pages = [pages]
    if isinstance(pages, (list, tuple)):
        values.extend(pages)
    for key in ("ir_url", "investor_relations_url"):
        if company.get(key):
            values.append(company[key])
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, dict):
            value = value.get("url") or value.get("href")
        if not value:
            continue
        url, _ = urldefrag(str(value).strip())
        safe, _ = _safe_http_url(url)
        if safe and url not in seen:
            seen.add(url)
            result.append(url)
    return result


def _same_official_host(url: str, roots: list[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    root_hosts = {(urlparse(root).hostname or "").lower() for root in roots}
    return host in root_hosts or any(host.endswith("." + root) for root in root_hosts if root)


def _is_navigation_html(url: str, title: str) -> bool:
    """Recognize HTML index/navigation links that should be crawled as pages.

    Investor-relations sites commonly expose their indexes as extensionless
    routes or ``.aspx`` pages.  Those links can look like documents because
    their route contains words such as ``presentation`` or ``annual report``.
    Keep a real release HTML link (including one nested below an events route)
    eligible by requiring an explicit material cue and period before treating
    a generic route as a document.
    """

    parsed = urlparse(url)
    ext = Path(parsed.path).suffix.lower()
    # Binary links are source documents even when their path contains a
    # navigation marker such as ``events-and-presentations``.
    if ext in {".pdf", ".xls", ".xlsx", ".csv", ".ppt", ".pptx", ".doc", ".docx"}:
        return False
    # The collector only follows HTML-like pages here.  Unknown extensions
    # remain document candidates; this preserves existing behavior for issuer
    # download endpoints while handling extensionless and .aspx pages below.
    if ext and ext not in {".html", ".htm", ".aspx", ".php", ".jsp", ".cfm", ".shtml"}:
        return False

    path = unquote(parsed.path)
    text = f"{title} {path} {parsed.query}".lower()
    generic = (
        "financial information",
        "events & presentations",
        "events-and-presentation",
        "events_and_presentation",
        "annual report & proxy",
        "annual-reports",
        "sec filings",
        "quarterly earnings",
        "press releases",
        "investor relations",
    )
    has_navigation_marker = any(value in text for value in generic)
    # A material link can live under a generic route.  Period plus a specific
    # material label distinguishes ``Q2 2026 Earnings Release`` from an
    # ``Events & Presentations`` index or an ``Annual Reports`` landing page.
    has_period = bool(re.search(r"\b(?:20\d{2}|q[1-4]|[1-4]q|fy\s*20?\d{2})\b", text, re.IGNORECASE))
    has_material = bool(re.search(
        r"\b(?:earnings?\s+(?:release|presentation|deck|call|transcript)|"
        r"(?:news|press)\s+release|financial\s+(?:results?|presentation|supplement)|"
        r"quarter(?:ly)?\s+results?|investor\s+presentation|annual\s+report|supplement)\b",
        text,
        re.IGNORECASE,
    ))
    if has_navigation_marker and has_period and has_material:
        return False
    if has_navigation_marker:
        return True
    # An event detail page often has an ``item=``/``event=`` query and should
    # be crawled to reach the issuer's actual PDF links.
    if parsed.query and any(key in parsed.query.lower() for key in ("item=", "event=", "page=")):
        if has_period and has_material:
            return False
        return True
    return False


def _page_links(page_url: str, content: bytes, roots: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """Return candidate documents and bounded follow-up official pages."""

    try:
        soup = BeautifulSoup(content, "html.parser")
    except Exception:
        return [], []
    documents: list[tuple[str, str]] = []
    pages: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url, _ = urldefrag(urljoin(page_url, href))
        if urlparse(url).scheme not in {"http", "https"}:
            continue
        title = " ".join(anchor.get_text(" ", strip=True).split())
        if looks_like_document(url, title) and not _is_navigation_html(url, title):
            documents.append((url, title))
            continue
        if _same_official_host(url, roots) and any(word in f"{title} {url}".lower() for word in ("earnings", "financial", "results", "events", "reports", "investor")):
            pages.append(url)
    return documents, pages


def _period_completed(period: str) -> bool:
    match = re.fullmatch(r"(20\d{2})-(?:Q([1-4])|FY)", period or "")
    if not match:
        return True
    year, quarter = int(match.group(1)), int(match.group(2) or 4)
    completed_year, completed_quarter = latest_completed_period()
    return (year, quarter) <= (completed_year, completed_quarter)


def _bound_document_links(
    config: dict[str, Any],
    links: list[tuple[str, str]],
    *,
    budget: int | None = None,
) -> list[tuple[str, str]]:
    """Bound a historical IR index to latest/prior reporting packages.

    IR pages commonly list years of releases and decks in one HTML response.
    Link titles/URLs are inspected before downloads, and only the latest two
    explicit periods plus same-quarter prior year are retained.  A few
    period-unknown links remain eligible for issuers whose page labels are
    generic; they are capped separately and never allowed to flood baseline
    coverage.
    """

    if not links:
        return []
    completed_year, completed_quarter = latest_completed_period()

    def completed(period: str) -> bool:
        match = re.fullmatch(r"(20\d{2})-(?:Q([1-4])|FY)", period or "")
        if not match:
            return period == "unknown"
        year = int(match.group(1))
        quarter = int(match.group(2) or 4)
        return (year, quarter) <= (completed_year, completed_quarter)

    period_by_link = [(url, title, infer_period(title, unquote(url))) for url, title in links]
    labels = sorted({period for _, _, period in period_by_link if period != "unknown"}, key=_period_order, reverse=True)
    labels = [period for period in labels if completed(period)]
    keep: set[str] = set(labels[:2])
    if labels:
        latest = labels[0]
        match = re.fullmatch(r"(20\d{2})-Q([1-4])", latest)
        if match:
            prior_year = f"{int(match.group(1)) - 1}-Q{match.group(2)}"
            if prior_year in labels:
                keep.add(prior_year)
        elif latest.endswith("-FY"):
            prior_year = f"{int(latest[:4]) - 1}-FY"
            if prior_year in labels:
                keep.add(prior_year)
    # Keep one latest completed annual package as structural context when a
    # quarterly page lists Q2/Q1/prior-year Q2 first.  Older annual links stay
    # out of the bounded baseline and therefore do not create alert noise.
    annual_labels = [label for label in labels if label.endswith("-FY")]
    annual_keep = {annual_labels[0]} if annual_labels else set()
    selected: list[tuple[str, str]] = []
    unknown_count = 0
    max_documents = _max_documents(config) if budget is None else max(0, min(_max_documents(config), int(budget)))
    if max_documents <= 0:
        return []
    # Preserve page order within each period because current issuer pages tend
    # to place the release before the presentation and supplement.  Process
    # explicit packages before generic links on the same page: generic
    # ``Press Release`` anchors often point at years of history and otherwise
    # consume the entire issuer budget before the current event link appears.
    prioritized = [
        item for item in period_by_link if item[2] in keep and completed(item[2])
    ] + [
        item for item in period_by_link if item[2] in annual_keep and item[2] not in keep and completed(item[2])
    ] + [item for item in period_by_link if item[2] == "unknown"]
    for url, title, period in prioritized:
        if (period in keep or period in annual_keep) and completed(period):
            selected.append((url, title))
        elif period == "unknown" and unknown_count < min(4, max_documents):
            selected.append((url, title))
            unknown_count += 1
        if len(selected) >= max_documents:
            break
    return selected


def _document_title(url: str, anchor_title: str, payload: bytes) -> str:
    if anchor_title.strip():
        return anchor_title.strip()
    name = Path(urlparse(url).path).name.replace("_", " ").replace("-", " ")
    if name:
        return name
    if payload.lstrip().startswith((b"<", b"<!DOCTYPE")):
        with contextlib.suppress(Exception):
            soup = BeautifulSoup(payload[:200_000], "html.parser")
            return soup.title.get_text(" ", strip=True) if soup.title else "Investor-relations material"
    return "Investor-relations material"


def _document_extension(url: str, content_type: str) -> str:
    """Prefer an explicit media type over a stale/misleading URL suffix."""

    extension = extension_for(url, content_type)
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    media_extension = {
        "application/pdf": ".pdf",
        "application/vnd.ms-excel": ".xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "application/vnd.ms-powerpoint": ".ppt",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
        "text/html": ".html",
        "text/plain": ".txt",
    }.get(media_type)
    return media_extension or extension


def _classification_link_text(url: str) -> str:
    """Return URL text useful for kind detection without route labels.

    A release file can sit below an ``events-and-presentations`` route.  The
    route word must not turn that release into a presentation; a descriptive
    filename such as ``Q2-2026-earnings-release`` still should.  The original
    URLs remain in ``Document`` provenance.
    """

    path = unquote(urlparse(url).path)
    return re.sub(
        r"(?:events[-_]and[-_]presentations?|annual[-_]reports?|investor[-_]relations|press[-_]releases?)",
        " ",
        path,
        flags=re.IGNORECASE,
    )


def _publication_date(fetched: _Fetched) -> str:
    """Return an issuer supplied publication date, when one is available.

    HTTP ``Last-Modified`` and ``Date`` describe transport/cache activity.  A
    CDN may update either header independently of when the issuer published an
    earnings package, so they must never be promoted to ``Document.published``.
    The current bounded IR collector does not infer dates from arbitrary PDF or
    HTML body text; callers can add a separately verified issuer date in a
    future source-specific parser.  Keeping this function as the single hook
    preserves the public source contract while making the unknown date
    explicit.
    """
    return ""


def _collect_document(
    config: dict[str, Any],
    company: dict[str, Any],
    fetched: _Fetched,
    anchor_title: str,
    discovered_from: str,
) -> Any:
    ticker = company_ticker(company)
    name = company_name(company, fallback=ticker)
    title = _document_title(fetched.final_url, anchor_title, fetched.content)
    content_type = _header(fetched.headers, "content-type")
    extension = _document_extension(fetched.final_url, content_type)
    # Keep the requested link in the period decision.  A CDN or issuer edge
    # can redirect an older PDF to a generic/current endpoint; the anchor
    # title and requested URL are the package's own period evidence.  The
    # final URL is a fallback for links whose title/request carries no period.
    period = infer_period(title, unquote(fetched.url), unquote(fetched.final_url))
    period_basis = "title_or_url"
    if period == "unknown" and extension in {".html", ".htm", ".txt", ".xml"}:
        period = infer_content_period(fetched.content[:30_000].decode("utf-8", errors="ignore"))
        if period != "unknown":
            period_basis = "opening_earnings_content"
    if period != "unknown" and not _period_completed(period):
        period = "unknown"
        period_basis = "future_period_deferred"
    # Use both requested and final links for classification.  A redirected
    # legacy deck can lose its descriptive filename at the CDN endpoint, but
    # the requested official link remains provenance for its presentation
    # kind (as it does for the period above).
    kind = title_kind(
        title,
        f"{_classification_link_text(fetched.url)} {_classification_link_text(fetched.final_url)}",
    )
    path, digest = archive_bytes(
        _storage(config),
        ticker=ticker,
        period=period,
        kind=kind,
        payload=fetched.content,
        extension=extension,
        content_hash=None,
    )
    return source_document(
        issuer=name,
        ticker=ticker,
        cik=company.get("cik", ""),
        title=title,
        kind=kind,
        source="ir",
        url=fetched.final_url,
        published=_publication_date(fetched),
        period=period,
        path=path,
        content_hash=digest,
        discovered_from=discovered_from,
        classification="issuer-published",
        mime_type=content_type.split(";", 1)[0].strip() or "application/octet-stream",
        metadata={
            "ticker": ticker,
            "official_ir_url": company.get("ir_url", ""),
            "discovered_from": discovered_from,
            "discovery_url": fetched.url,
            "final_url": fetched.final_url,
            "http_status": fetched.status_code,
            "content_type": content_type,
            "archive_extension": extension,
            # Transport headers are retained for cache/provenance audits but
            # are deliberately kept separate from issuer publication time.
            "http_last_modified": _header(fetched.headers, "last-modified"),
            "http_date": _header(fetched.headers, "date"),
            "publication_date_source": "issuer_explicit" if _publication_date(fetched) else "not_established",
            "period_basis": period_basis,
            "cdn_discovered_from_official_page": not _same_official_host(fetched.final_url, [str(x) for x in _urls(company)]),
        },
    )


def discover_ir(
    config: dict[str, Any],
    company: dict[str, Any],
    *,
    bootstrap: bool = False,
    checkpoints: dict[str, str] | None = None,
) -> SourceResult:
    """Discover current and prior issuer materials from official IR pages.

    The path is intentionally independent of SEC discovery: a newly published
    issuer presentation or release is retained even when EDGAR has not yet
    indexed its related filing.
    """

    result = SourceResult()
    ticker = company_ticker(company)
    source_key = f"{ticker}:ir"
    roots = _urls(company)
    if not ticker:
        result.errors.append({"source": "ir", "source_key": source_key, "error": "company ticker is missing"})
        return result
    if not roots:
        configured = bool(company.get("ir_url") or company.get("investor_relations_url") or company.get("ir_pages"))
        if configured:
            result.errors.append({
                "source": "ir",
                "source_key": source_key,
                "ticker": ticker,
                "error": "configured investor-relations URL is not a safe public HTTP URL",
                "retryable": False,
            })
        else:
            result.pending.append({"source": "ir", "source_key": source_key, "ticker": ticker, "reason": "official investor-relations URL is not configured"})
        return result
    manifest = _load_manifest(config)
    pending_seed_urls: list[str] = []
    current_cik = normalize_cik(company.get("cik"))
    pending_rows = config.get("_pending", [])
    if isinstance(pending_rows, list):
        for row in pending_rows:
            if not isinstance(row, dict) or row.get("source_key") != source_key:
                continue
            pending_cik = normalize_cik(row.get("cik"))
            if pending_cik and pending_cik != current_cik:
                continue
            candidate = str(row.get("url") or "").strip()
            safe, _ = _safe_http_url(candidate)
            if safe and candidate not in pending_seed_urls:
                pending_seed_urls.append(candidate)
    # Retry persisted document URLs directly as well as rediscovering them
    # from the current IR pages.  Issuers often remove older links from an
    # index after a release, so a page-only retry can otherwise lose durable
    # pending work once the checkpoint moves.
    queue = list(roots) + pending_seed_urls
    queued = set(queue)
    visited: set[str] = set()
    attempted_documents: set[str] = set()
    unknown_documents = 0
    max_unknown_documents = max(1, min(4, _max_documents(config) // 3))
    blocked_stop = False
    robots_cache: dict[str, tuple[RobotFileParser | None, int | None]] = {}
    try:
        with httpx.Client(follow_redirects=True, timeout=_request_timeout(config)) as session:
            while queue and len(visited) < _max_pages(config):
                page_url = queue.pop(0)
                if page_url in visited:
                    continue
                visited.add(page_url)
                try:
                    fetched = _fetch(config, session, robots_cache, manifest, page_url)
                except Exception as exc:
                    entry = {
                        "source": "ir",
                        "source_key": source_key,
                        "ticker": ticker,
                        "url": page_url,
                        "error": _safe_error(exc),
                        "blocked": bool(getattr(exc, "blocked", False)),
                        "status_code": getattr(exc, "status_code", None),
                        "retryable": not bool(getattr(exc, "blocked", False)),
                    }
                    result.errors.append(entry)
                    if bool(getattr(exc, "blocked", False)):
                        blocked_stop = True
                        break
                    continue
                content_type = _header(fetched.headers, "content-type").lower()
                if looks_like_document(fetched.final_url, "", content_type) and (Path(urlparse(fetched.final_url).path).suffix.lower() in {".pdf", ".xls", ".xlsx", ".ppt", ".pptx", ".doc", ".docx"} or "application/pdf" in content_type):
                    if fetched.final_url in attempted_documents or page_url in attempted_documents:
                        continue
                    period_hint = infer_period("", unquote(fetched.final_url))
                    if period_hint == "unknown" and unknown_documents >= max_unknown_documents:
                        continue
                    if period_hint == "unknown":
                        unknown_documents += 1
                    attempted_documents.add(fetched.final_url)
                    try:
                        result.documents.append(_collect_document(config, company, fetched, "", page_url))
                    except Exception as exc:
                        result.errors.append({"source": "ir", "source_key": source_key, "ticker": ticker, "url": page_url, "error": _safe_error(exc), "blocked": bool(getattr(exc, "blocked", False)), "retryable": False})
                        if bool(getattr(exc, "blocked", False)):
                            blocked_stop = True
                            break
                    continue
                docs, follow_pages = _page_links(fetched.final_url, fetched.content, roots)
                remaining_budget = max(0, _max_documents(config) - len(attempted_documents))
                bounded_docs = _bound_document_links(config, docs, budget=remaining_budget)
                for doc_index, (doc_url, anchor_title) in enumerate(bounded_docs):
                    if doc_url in visited or doc_url in attempted_documents:
                        continue
                    period_hint = infer_period(anchor_title, unquote(doc_url))
                    if period_hint == "unknown":
                        if unknown_documents >= max_unknown_documents:
                            continue
                        unknown_documents += 1
                    attempted_documents.add(doc_url)
                    try:
                        document_response = _fetch(config, session, robots_cache, manifest, doc_url)
                        result.documents.append(_collect_document(config, company, document_response, anchor_title, fetched.final_url))
                    except Exception as exc:
                        result.errors.append({
                            "source": "ir",
                            "source_key": source_key,
                            "ticker": ticker,
                            "url": doc_url,
                            "discovered_from": fetched.final_url,
                            "error": _safe_error(exc),
                            "blocked": bool(getattr(exc, "blocked", False)),
                            "status_code": getattr(exc, "status_code", None),
                            "retryable": not bool(getattr(exc, "blocked", False)),
                        })
                        if bool(getattr(exc, "blocked", False)):
                            blocked_stop = True
                            for deferred_url, _deferred_title in bounded_docs[doc_index + 1 :]:
                                result.pending.append({
                                    "source": "ir",
                                    "source_key": source_key,
                                    "ticker": ticker,
                                    "url": deferred_url,
                                    "discovered_from": fetched.final_url,
                                    "reason": "deferred after issuer IR access block",
                                })
                            break
                if blocked_stop:
                    break
                for follow_page in follow_pages:
                    if follow_page not in queued and follow_page not in visited:
                        queued.add(follow_page)
                        queue.append(follow_page)
    except Exception as exc:
        result.errors.append({"source": "ir", "source_key": source_key, "ticker": ticker, "error": _safe_error(exc), "retryable": False})
    finally:
        with contextlib.suppress(Exception):
            write_json_atomic(_manifest_path(config), manifest)
    if blocked_stop:
        # Preserve pages that were discovered but not fetched.  A later run
        # can retry them after the issuer's access policy changes; no
        # checkpoint is advanced while this durable work remains.
        for pending_url in queue:
            result.pending.append({
                "source": "ir",
                "source_key": source_key,
                "ticker": ticker,
                "url": pending_url,
                "reason": "deferred after issuer IR access block",
            })
    if visited:
        result.checked.append(ticker)
        if not result.errors and not result.pending:
            result.checkpoints[source_key] = utc_now()
    elif not result.errors:
        result.pending.append({"source": "ir", "source_key": source_key, "ticker": ticker, "reason": "official IR pages were not fetched"})
    return result


def doctor_ir(config: dict[str, Any], company: dict[str, Any], *, live: bool = False) -> dict[str, Any]:
    """Return safe IR diagnostics, optionally probing the first official page.

    ``doctor --offline`` never calls this function.  Callers that run the
    live doctor pass ``live=True`` only for enabled issuers; disabled watchlist
    candidates remain configuration-only so a doctor invocation does not
    probe every optional site.
    """

    ticker = company_ticker(company)
    urls = _urls(company)
    output = {
        "source": "ir",
        "ticker": ticker,
        "configured": bool(urls),
        "official_pages": len(urls),
        "robots_respected": True,
        "user_agent": _user_agent(config),
        "access_tested": False,
        "access_status": "not_tested",
    }
    if not urls or not live:
        if not urls:
            output["access_status"] = "not_configured"
        return output

    manifest: dict[str, dict[str, Any]] = {}
    robots_cache: dict[str, tuple[RobotFileParser | None, int | None]] = {}
    try:
        # A doctor probe uses the same bounded transport/robots path as
        # collection, but fetches only the configured primary page and never
        # follows document links or archives source material.
        with httpx.Client(follow_redirects=True, timeout=_request_timeout(config)) as session:
            fetched = _fetch(config, session, robots_cache, manifest, urls[0])
        output.update({
            "access_tested": True,
            "access_status": "ok" if fetched.status_code == 200 else "unexpected_status",
            "status_code": fetched.status_code,
            "checked_url": fetched.final_url,
        })
    except Exception as exc:
        output.update({
            "access_tested": True,
            "access_status": "blocked" if bool(getattr(exc, "blocked", False)) else "error",
            "status_code": getattr(exc, "status_code", None),
            "error": _safe_error(exc),
            "checked_url": urls[0],
        })
    return output
