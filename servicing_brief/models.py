"""Shared contracts. Financial amounts are decimal strings, never floats."""
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Document:
    issuer: str
    cik: str
    title: str
    kind: str  # presentation, release, supplement, 10-Q, 10-K, 8-K, amendment, delay
    source: str  # sec or ir
    url: str
    published: str
    period: str  # YYYY-Qn, YYYY-FY, or unknown (never guessed from filing date)
    path: str
    content_hash: str
    accession: str = ""
    accepted: str = ""
    retrieved: str = field(default_factory=utcnow)
    discovered: str = field(default_factory=utcnow)
    discovered_from: str = ""
    classification: str = "issuer-published"
    mime_type: str = "application/octet-stream"
    id: str = ""
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.id:
            import hashlib
            from .document_identity import asset_manifest, content_fingerprint
            identity_hash = self.content_hash
            try:
                if asset_manifest(self):
                    identity_hash = content_fingerprint(self)
            except (ValueError, TypeError):
                # Keep malformed source observations representable so State
                # can quarantine them independently and retain retry details.
                pass
            self.id = hashlib.sha256((self.cik + "|" + self.url + "|" + identity_hash).encode()).hexdigest()[:24]

    def to_dict(self):
        return asdict(self)


@dataclass
class SourceResult:
    documents: list[Document] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    pending: list[dict] = field(default_factory=list)
    checkpoints: dict[str, str] = field(default_factory=dict)
    checked: list[str] = field(default_factory=list)
