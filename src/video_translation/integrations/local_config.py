"""Load only the project's explicit local integration settings.

Environment variables win. Never reads a sibling project's configuration implicitly.
Secrets may be referenced from a user-selected text/JSON file without copying them.
"""

from __future__ import annotations
import json
import os
from pathlib import Path


def load_local_environment(root: Path, env=None):
    env = os.environ if env is None else env
    if env.get("VT_IGNORE_LOCAL_CONFIG") == "1":
        return
    path = root / "config/application/local.json"
    if not path.exists():
        return
    try:
        config = json.loads(path.read_text("utf-8-sig"))
        values = config.get("environment", {})
        refs = config.get("secret_files", {})
        if not isinstance(values, dict) or not isinstance(refs, dict):
            raise ValueError()
        prepared = {}
        for key, value in values.items():
            if not key.startswith("VT_") or not isinstance(value, str):
                raise ValueError()
            prepared[key] = value.replace("${PROJECT_ROOT}", str(root))
        for key, spec in refs.items():
            if key in env:
                continue
            if not key.startswith("VT_") or not isinstance(spec, dict):
                raise ValueError()
            source = Path(spec["path"].replace("${PROJECT_ROOT}", str(root))).expanduser()
            if not source.is_absolute():
                source = root / source
            if not source.is_file():
                continue  # Unconfigured is surfaced in capabilities; no secret is fabricated.
            if source.stat().st_size > 65536:
                raise ValueError()
            text = source.read_text("utf-8-sig").strip()
            if spec.get("format", "text") == "json":
                value = json.loads(text)
                for component in spec["key"].split("."):
                    value = value[component]
            else:
                value = text
            if not isinstance(value, str) or len(value) > 16384:
                raise ValueError()
            prepared[key] = value
        for key, value in prepared.items():
            env.setdefault(key, value)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ValueError(
            "本机接入配置格式或引用文件无效；检查 config/application/local.json（不会回显秘密）"
        ) from None
