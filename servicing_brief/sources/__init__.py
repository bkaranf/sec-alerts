"""Authoritative SEC and issuer investor-relations document collection.

The source layer deliberately keeps acquisition separate from extraction.  SEC
work is performed through EdgarTools only, while issuer materials are found
from configured official IR pages and fetched with a small, polite HTTP client.
Both paths return :class:`~servicing_brief.models.Document` objects containing
the archived original bytes and provenance needed by later stages.
"""

from __future__ import annotations

__all__ = ["collect", "doctor_sources", "discover_ir", "discover_sec"]


def __getattr__(name: str):
    """Load a source entry point only when a caller asks for it.

    Keeping the package exports lazy avoids importing both network clients and
    both source backends for callers that only need one source or only inspect
    the package.  The attributes are intentionally not cached here so patches
    made to the concrete source modules remain visible to existing callers.
    """

    if name in {"collect", "doctor_sources"}:
        from . import collector

        return getattr(collector, name)
    if name == "discover_ir":
        from . import ir

        return ir.discover_ir
    if name == "discover_sec":
        from . import sec

        return sec.discover_sec
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
