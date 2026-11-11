"""Explicit, secret-free local MCP configuration. Does not enable desktop plugins.

Examples:
  python scripts/configure_fireplay.py --service ai_designer --write
  python scripts/configure_fireplay.py --plugin-patch PATH/cordis.patch.yml --write
  python scripts/configure_fireplay.py --probe

Only URL/server metadata is imported from a user-selected public bundle. YAML JS
expressions remain inert text; credentials and environment values are never copied.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from video_translation.integrations.fireplay.mcp import (  # noqa: E402
    FireplayMCP, FireplayMCPConfig, FireplayMCPError, SERVICES,
)


def import_public_endpoint(path: Path, service: str) -> str:
    """Read only an explicitly selected small YAML bundle; never evaluate !!js."""
    try:
        path = path.expanduser().resolve()
        if "app.asar" in path.parts or path.name != "cordis.patch.yml" or path.stat().st_size > 131072:
            raise ValueError()
        document = yaml.load(path.read_text("utf-8-sig"), Loader=yaml.BaseLoader)
        if not isinstance(document, list):
            raise ValueError()
        matches = []
        for entry in document:
            if not isinstance(entry, dict):
                continue
            for plugin in entry.get("insert", []):
                if not isinstance(plugin, dict) or plugin.get("name") != "@deepseek-ai/dsh-mcp-client":
                    continue
                config = plugin.get("config", {})
                if isinstance(config, dict) and config.get("serverName") == service:
                    if config.get("transport") != "streamable-http":
                        raise ValueError()
                    matches.append(config.get("url"))
        if len(matches) != 1 or matches[0] != SERVICES[service][0]:
            raise ValueError()
        return matches[0]
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError):
        raise FireplayMCPError("invalid_plugin", "公开 MCP 插件未包含唯一、可信的目标服务地址") from None


def write_config(root: Path, config: FireplayMCPConfig, *, replace_existing: bool = False) -> Path:
    target = root / "runtime/config/fireplay.json"
    if target.exists() and not replace_existing:
        raise FireplayMCPError("config_exists", "本机 Fireplay 配置已存在；确认替换时添加 --replace")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Persist references only, even if the current environment already has a token.
    encoded = json.dumps(config.public_dict(), indent=2, ensure_ascii=True) + "\n"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
        if target.exists() and not replace_existing:
            raise FireplayMCPError("config_exists", "本机 Fireplay 配置已存在")
        os.replace(temporary, target)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Configure project-owned Fireplay MCP without copying secrets")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--service", choices=tuple(SERVICES), default=None)
    parser.add_argument("--token-env", help="Environment variable NAME only; never pass a token value")
    parser.add_argument("--plugin-patch", type=Path, help="Explicit public plugin cordis.patch.yml path")
    parser.add_argument("--write", action="store_true", help="Save secret-free runtime/config/fireplay.json")
    parser.add_argument("--replace", action="store_true", help="Allow replacing an existing local config")
    parser.add_argument("--disable", action="store_true")
    parser.add_argument("--probe", action="store_true", help="Initialize and discover tools only; no business calls")
    args = parser.parse_args(argv)
    from video_translation.integrations.local_config import load_local_environment
    load_local_environment(args.root)
    try:
        root = args.root.expanduser().resolve()
        current = FireplayMCPConfig.from_env(root)
        service = args.service or current.service
        config = FireplayMCPConfig(
            enabled=not args.disable, service=service,
            url=import_public_endpoint(args.plugin_patch, service) if args.plugin_patch else SERVICES[service][0],
            token_env=args.token_env or (current.token_env if service == current.service else SERVICES[service][1]),
            timeout_seconds=current.timeout_seconds, max_response_bytes=current.max_response_bytes,
        )
        if args.write:
            write_config(root, config, replace_existing=args.replace)
        elif not args.plugin_patch and not args.service and not args.token_env:
            config = replace(current, enabled=not args.disable) if args.probe else current
        with FireplayMCP(config) as client:
            result = client.status(probe=args.probe)
        result["saved"] = args.write
        result["purpose"] = "requirements" if service == "ai_designer" else "campaign_diagnostics"
        print(json.dumps(result, ensure_ascii=True))
        return 0 if not args.probe or result.get("connected") else 2
    except FireplayMCPError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=True))
        return 2
    except OSError:
        print(json.dumps({"code": "configuration_io_error", "message": "本机配置无法写入"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
