"""Report configuration separately from actual provider/quality verification."""

from __future__ import annotations
import json
import os
import shutil
import subprocess
from pathlib import Path
from video_translation.integrations.agents.adapter import AgentConfig
from video_translation.integrations.agents.errors import CapabilityUnavailable


def configuration_status(root: Path, engine=None):
    engine = engine or os.environ.get("VT_AGENT_ENGINE", "custom")
    config = AgentConfig.from_env({**os.environ, "VT_AGENT_ENGINE": engine, "VT_ROOT": str(root)})
    if engine == "harness":
        installed = Path(config.command[0]).is_file()
        auth = bool(config.env.get("DEEPSEEK_API_KEY"))
    elif engine == "codex":
        installed = bool(
            os.environ.get("VT_CODEX_BIN")
            or shutil.which("codex")
            or (root / "runtime/tools/codex/node_modules/@openai/codex/bin/codex.js").is_file()
        )
        auth = bool(
            config.env.get("OPENAI_API_KEY")
            or (Path(config.env.get("CODEX_HOME", root / "runtime/codex-home")) / "auth.json").is_file()
        )
    else:
        installed = (
            config.mode == "http"
            and bool(config.base_url and config.model)
            or config.mode == "command"
            and bool(config.command)
        )
        auth = bool(installed)
    return {
        "engine": engine,
        "installed": bool(installed),
        "credential_configured": auth,
        "configured": bool(installed and auth),
        "live_model_verified": False,
        "work_tool_profile": "native_codex_tools" if engine == "codex" else "structured",
        "native_imagegen_live_verified": False,
    }


def ensure_engine_ready(root: Path, engine):
    state = configuration_status(root, engine)
    if not state["configured"]:
        raise CapabilityUnavailable("制作前检查：所选Agent运行组件或身份配置缺失，未发送付费请求")
    if engine == "codex":
        config = AgentConfig.from_env({**os.environ, "VT_AGENT_ENGINE": engine, "VT_ROOT": str(root)})
        env = {
            k: v
            for k, v in os.environ.items()
            if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT"}
        }
        env.update(config.env)
        try:
            result = subprocess.run(
                [*config.command[:2], "--check"],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=30,
                shell=False,
            )
            data = json.loads(result.stdout)
            if result.returncode or not data.get("available"):
                raise ValueError()
        except (OSError, subprocess.TimeoutExpired, ValueError):
            raise CapabilityUnavailable(
                "制作前检查：Codex CLI或登录不可用，尚未上传素材或发送模型请求"
            ) from None
