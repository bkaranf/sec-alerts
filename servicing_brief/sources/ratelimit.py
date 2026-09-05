"""Cross-process SEC acquisition guard and EdgarTools rate configuration."""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
from pathlib import Path
from typing import Iterator

from filelock import FileLock, Timeout


SEC_RPS_LIMIT = 5


def shared_sec_lock_path(lock_path: str | os.PathLike[str] | None = None) -> Path:
    """Return the per-user lock used by every SEC source invocation."""

    if lock_path:
        return Path(lock_path).expanduser().resolve()
    shared_root = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    if shared_root:
        return Path(shared_root).resolve() / "MortgageServicingBrief" / "sec-acquisition.lock"
    return Path(tempfile.gettempdir()).resolve() / "MortgageServicingBrief" / "sec-acquisition.lock"


def configure_edgartools_rate_limit(limit: int = SEC_RPS_LIMIT) -> dict[str, object]:
    """Set EdgarTools' documented rate setting before live SEC work.

    EdgarTools reads ``EDGAR_RATE_LIMIT_PER_SEC`` when its HTTP manager is
    created.  The environment variable is therefore set before the lazy
    ``edgar`` import in this project.  If another component imported the
    library first, use its documented runtime rate update when available; if
    the installed version cannot prove a <=5 request/second manager, fail
    closed instead of silently allowing a higher rate.
    """

    bounded = max(1, min(int(limit or SEC_RPS_LIMIT), SEC_RPS_LIMIT))
    os.environ["EDGAR_RATE_LIMIT_PER_SEC"] = str(bounded)
    details: dict[str, object] = {
        "configured": True,
        "limit": bounded,
        "manager_refreshed": False,
        "manager_verified": "edgar" not in sys.modules,
    }
    if "edgar" in sys.modules:
        try:
            import edgar.httpclient as httpclient

            updater = getattr(httpclient, "update_rate_limiter", None)
            if callable(updater):
                updater(requests_per_second=bounded)
                details["manager_refreshed"] = True
                details["manager_verified"] = True
            else:
                manager = getattr(httpclient, "HTTP_MGR", None)
                current = getattr(manager, "request_per_sec_limit", None)
                if current is not None and int(current) <= bounded:
                    details["manager_verified"] = True
                else:
                    details["manager_verified"] = False
                    details["manager_error"] = "EdgarTools was imported before the <=5 request/second setting"
        except Exception as exc:  # pragma: no cover - depends on optional runtime version
            details["manager_verified"] = False
            details["manager_error"] = type(exc).__name__
    return details


@contextlib.contextmanager
def sec_acquisition_guard(
    storage: str | os.PathLike[str],
    timeout: float = 0.0,
    lock_path: str | os.PathLike[str] | None = None,
) -> Iterator[dict[str, object]]:
    """Serialize all live SEC work across processes.

    Holding the lock across discovery and document acquisition makes the
    aggregate budget enforceable even when two scheduled invocations overlap.
    The caller receives a small status dictionary and can turn lock timeout
    into a source error without printing identity or other secrets.
    """

    # All project storage roots for this Windows user share one lock.  A
    # caller may provide an explicit coordination path for a deployment that
    # already manages a shared state directory.
    resolved_lock_path = shared_sec_lock_path(lock_path)
    resolved_lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(resolved_lock_path))
    acquired = False
    status: dict[str, object] = {"lock_path": str(resolved_lock_path), "acquired": False}
    try:
        try:
            lock.acquire(timeout=max(0.0, float(timeout)))
            acquired = True
            status["acquired"] = True
            status.update(configure_edgartools_rate_limit())
        except Timeout:
            status["error"] = "SEC acquisition lock is held by another process"
        yield status
    finally:
        if acquired:
            lock.release()
