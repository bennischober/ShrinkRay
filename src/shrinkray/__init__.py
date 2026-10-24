"""ShrinkRay - quality-measured video library compressor.

The version lives in pyproject.toml (single source of truth) and is read
from the installed package metadata; the fallback covers direct src-tree
runs without an installed dist metadata.
"""

from importlib import metadata

try:
    __version__ = metadata.version("shrinkray")
except metadata.PackageNotFoundError:  # direct src-tree run without install
    __version__ = "0.4.0"
