"""Compatibility import for the independent media font library."""
from media_capabilities.fonts import (
    REFERENCES, SYSTEM_FONTS, catalog, discover as _discover, missing_characters,
)
from pathlib import Path

__all__ = ["REFERENCES", "SYSTEM_FONTS", "catalog", "discover", "missing_characters"]


def discover(root, explicit=None):
    return _discover(Path(root) / "config" / "fonts", explicit)
