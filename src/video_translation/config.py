from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    root: Path
    runtime: Path
    max_concurrent_tasks: int = 2
    max_concurrent_nodes: int = 2
    max_upload_bytes: int = 512 * 1024 * 1024
    max_duration_ms: int = 180_000
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    worker_enabled: bool = True
    fireplay_url: str = ""
    fireplay_token: str = ""
    fireplay_scope: str = ""
    fireplay_mode: str = "bridge"
    server_host: str = "127.0.0.1"
    lan_hosts: tuple[str, ...] = ()
    lan_networks: tuple[str, ...] = ()

    @classmethod
    def load(cls, root: Path | None = None, runtime: Path | None = None, worker_enabled=True):
        root = (root or Path(os.environ.get("VT_ROOT", Path(__file__).resolve().parents[2]))).resolve()
        from video_translation.integrations.local_config import load_local_environment
        load_local_environment(root)
        path = root / "config/application/runtime.toml"
        config = tomllib.loads(path.read_text("utf-8")) if path.exists() else {}
        server = config.get("server", {})
        return cls(
            root=root,
            runtime=(runtime or Path(os.environ.get("VT_RUNTIME", root / "runtime"))).resolve(),
            max_concurrent_tasks=max(1, min(8, int(config.get("max_concurrent_tasks", 2)))),
            max_concurrent_nodes=max(1, min(4, int(config.get("max_concurrent_nodes", 2)))),
            max_upload_bytes=int(config.get("max_upload_bytes", 512 * 1024 * 1024)),
            max_duration_ms=int(config.get("max_duration_ms", 180_000)),
            ffmpeg=os.environ.get("VT_FFMPEG", "ffmpeg"),
            ffprobe=os.environ.get("VT_FFPROBE", "ffprobe"),
            worker_enabled=worker_enabled,
            fireplay_url=os.environ.get("VT_FIREPLAY_BRIDGE_URL", ""),
            fireplay_token=os.environ.get("VT_FIREPLAY_BRIDGE_TOKEN", ""),
            fireplay_scope=os.environ.get("VT_FIREPLAY_SCOPE", "all"),
            fireplay_mode=os.environ.get("VT_FIREPLAY_MODE", "bridge" if os.environ.get("VT_FIREPLAY_BRIDGE_URL") else "mcp"),
            server_host=server.get("host", "127.0.0.1"),
            lan_hosts=tuple(server.get("allowed_hosts", [])),
            lan_networks=tuple(server.get("allowed_networks", [])),
        )

    def media_available(self):
        return bool(shutil.which(self.ffmpeg) and shutil.which(self.ffprobe))
