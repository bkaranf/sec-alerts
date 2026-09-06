"""Historical entry point with independent renderer globals and CLI defaults."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import FunctionType

_LEGACY_ROOT = Path(__file__).resolve().parent
_REPOSITORY_ROOT = _LEGACY_ROOT.parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

# Execute one fresh source module per import, then bind its own functions to
# this namespace. Legacy overrides work directly, without changing another
# renderer, temporarily patching globals, or serializing concurrent calls.
_spec = importlib.util.spec_from_file_location(
    "servicing_brief._legacy_review_rendering", _REPOSITORY_ROOT / "servicing_brief/review_rendering.py"
)
if _spec is None or _spec.loader is None:
    raise ImportError("Cannot load packaged review renderer")
_impl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_impl)
_exports = {name: value for name, value in vars(_impl).items() if not name.startswith("__")}
for _name, _value in _exports.items():
    if isinstance(_value, FunctionType) and _value.__module__ == _impl.__name__:
        _function = FunctionType(_value.__code__, globals(), _name, _value.__defaults__, _value.__closure__)
        _function.__kwdefaults__ = _value.__kwdefaults__
        _function.__annotations__ = _value.__annotations__
        _function.__dict__.update(_value.__dict__)
        _function.__doc__ = _value.__doc__
        globals()[_name] = _function
    else:
        globals()[_name] = _value

_create_environment = create_review_environment
_run = main


def create_review_environment(template_root: str | Path | None = None):
    return _create_environment(_LEGACY_ROOT if template_root is None else template_root)


def main(argv: list[str] | None = None) -> int:
    return _run(argv, default_review_root=_LEGACY_ROOT, template_root=_LEGACY_ROOT)


__all__ = [name for name in _exports if not name.startswith("_")]

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        raise SystemExit(f"render_email: {exc}")
