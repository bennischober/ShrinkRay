"""Transitional re-export shim: moved to shrinkray.server.jobs (issue #97 phase 4).
Removed when import-linter lands (phase 6)."""

import importlib

_mod = importlib.import_module("shrinkray.server.jobs")
_public = [n for n in dir(_mod) if not n.startswith("_")]
for _n in _public:
    globals()[_n] = getattr(_mod, _n)
