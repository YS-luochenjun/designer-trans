"""Read the small versioned catalogs bundled with this library."""
from copy import deepcopy
from functools import lru_cache
import hashlib
from importlib.resources import files
import json

_NAMES = frozenset({"locales", "fonts"})


@lru_cache(maxsize=2)
def _load(name):
    if name not in _NAMES:
        raise ValueError(f"Unknown media catalog: {name}")
    raw = files("media_capabilities").joinpath("data", name + ".json").read_bytes()
    data = json.loads(raw)
    return data, {"id": data["id"], "version": data["version"],
                  "sha256": hashlib.sha256(raw).hexdigest()}


def read_catalog(name):
    """Return detached catalog data; callers cannot alter the shared definition."""
    return deepcopy(_load(name)[0])


def catalog_info(name):
    """Identify the exact catalog without storing workflow or task state."""
    return dict(_load(name)[1])
