"""Compatibility entry point for the packaged review renderer.

The reusable implementation lives in :mod:`servicing_brief.review_rendering`.
This checkout-local module keeps the historical script path, private helper
names, template defaults, and command-line behavior available to old callers.
"""

from __future__ import annotations

import functools
import importlib.util
import inspect
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

_LEGACY_ROOT = Path(__file__).resolve().parent
_REPOSITORY_ROOT = _LEGACY_ROOT.parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

_PRIVATE_IMPL_NAME = f"servicing_brief._legacy_review_rendering_{uuid4().hex}"
_PRIVATE_IMPL_PATH = _REPOSITORY_ROOT / "servicing_brief" / "review_rendering.py"
_PRIVATE_IMPL_SPEC = importlib.util.spec_from_file_location(_PRIVATE_IMPL_NAME, _PRIVATE_IMPL_PATH)
if _PRIVATE_IMPL_SPEC is None or _PRIVATE_IMPL_SPEC.loader is None:
    raise ImportError(f"Cannot load packaged renderer from {_PRIVATE_IMPL_PATH}")
_impl = importlib.util.module_from_spec(_PRIVATE_IMPL_SPEC)
_PRIVATE_IMPL_SPEC.loader.exec_module(_impl)


# Re-export the historical module surface.  Functions dispatch through the
# package module so callers that monkeypatch an old helper still affect the
# package implementation's internal calls, as they did before the move.
_FORWARDED_NAMES = tuple(
    name
    for name in _impl.__dict__
    if not name.startswith("__") and name not in {"_impl"}
)
_ORIGINALS = {name: getattr(_impl, name) for name in _FORWARDED_NAMES}
_MISSING = object()
_PATCH_LOCK = threading.RLock()


@contextmanager
def _patched_implementation():
    """Apply legacy-module patches only for the duration of one call."""
    with _PATCH_LOCK:
        changed: dict[str, object] = {}
        try:
            for name, baseline in _ADAPTER_BASELINES.items():
                current = globals().get(name, _MISSING)
                if current is baseline:
                    continue
                changed[name] = getattr(_impl, name, _MISSING)
                if current is _MISSING:
                    if hasattr(_impl, name):
                        delattr(_impl, name)
                else:
                    setattr(_impl, name, current)
            yield
        finally:
            for name, previous in changed.items():
                if previous is _MISSING:
                    if hasattr(_impl, name):
                        delattr(_impl, name)
                else:
                    setattr(_impl, name, previous)


def _call_implementation(name: str, *args, **kwargs):
    with _patched_implementation():
        return getattr(_impl, name)(*args, **kwargs)


for _name in _FORWARDED_NAMES:
    _value = _ORIGINALS[_name]
    if inspect.isfunction(_value) and _name not in {"create_review_environment", "main"}:
        def _forward(*args, __name=_name, **kwargs):
            return _call_implementation(__name, *args, **kwargs)

        _forward = functools.update_wrapper(_forward, _value)
        globals()[_name] = _forward
    else:
        globals()[_name] = _value


def create_review_environment(template_root: str | Path | None = None):
    """Keep the old template-root default while using the package factory."""
    return _call_implementation(
        "create_review_environment",
        _LEGACY_ROOT if template_root is None else template_root,
    )


def main(argv: list[str] | None = None) -> int:
    """Run the historical CLI with defaults rooted beside this adapter."""
    return _call_implementation(
        "main",
        argv,
        default_review_root=_LEGACY_ROOT,
        template_root=_LEGACY_ROOT,
    )


def __getattr__(name: str):
    return getattr(_impl, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_impl)))


__all__ = [name for name in _FORWARDED_NAMES if not name.startswith("_")] + [
    "create_review_environment",
    "main",
]


_ADAPTER_BASELINES = {
    name: globals().get(name, _MISSING)
    for name in _FORWARDED_NAMES
}
_ADAPTER_BASELINES.update(
    create_review_environment=create_review_environment,
    main=main,
)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        raise SystemExit(f"render_email: {exc}")
