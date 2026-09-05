"""Authoritative SEC and issuer investor-relations document collection.

The source layer deliberately keeps acquisition separate from extraction.  SEC
work is performed through EdgarTools only, while issuer materials are found
from configured official IR pages and fetched with a small, polite HTTP client.
Both paths return :class:`~servicing_brief.models.Document` objects containing
the archived original bytes and provenance needed by later stages.
"""

from __future__ import annotations

from .collector import collect, doctor_sources
from .ir import discover_ir
from .sec import discover_sec

__all__ = ["collect", "doctor_sources", "discover_ir", "discover_sec"]

