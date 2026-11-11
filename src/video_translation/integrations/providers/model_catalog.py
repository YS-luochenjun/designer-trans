"""Human-edited model catalog and immutable, credential-free task selections."""

from __future__ import annotations
import hashlib
import json
from pathlib import Path

PROTOCOLS = {
    "transcription": {"kie_chat_audio_v1"},
    "tts": {"kie_gemini_tts_v1", "kie_gemini38_tts_v1"},
    "video": {"kie_wan_edit_v1", "kie_wan_reference_v1"},
}


def resolve_selection(root: Path, selection=None):
    catalog = json.loads((root / "config/models/catalog.json").read_text("utf-8"))
    selection = selection or json.loads((root / "config/models/selection.json").read_text("utf-8"))
    if not isinstance(selection, dict) or set(selection) - {
        "version",
        "transcription",
        "tts",
        "video",
        "options",
    }:
        raise ValueError("模型选择只能包含version、三种模型和options，不允许密钥或任意字段")
    if not isinstance(selection.get("options", {}), dict) or set(selection.get("options", {})) - set(
        PROTOCOLS
    ):
        raise ValueError("模型参数必须按transcription/tts/video分组")
    result = {}
    for role, protocols in PROTOCOLS.items():
        key = selection[role]
        spec = catalog["models"].get(key)
        if not spec or spec.get("role") != role or spec.get("protocol") not in protocols:
            raise ValueError(f"{role}模型不存在或接口协议尚未实现；不能仅替换model ID跨接口使用")
        if not isinstance(spec.get("model"), str) or not spec["model"]:
            raise ValueError("模型ID不能为空")
        options = {**spec.get("defaults", {}), **selection.get("options", {}).get(role, {})}
        allowed = {
            "transcription": set(),
            "tts": {"voice", "accent", "temperature", "scene"},
            "video": {"resolution", "prompt_extend", "audio_setting", "duration"},
        }[role]
        if spec["protocol"] == "kie_gemini38_tts_v1":
            allowed = {"voice", "temperature"}
        if set(options) - allowed:
            raise ValueError(f"{role}包含未支持的参数")
        if role == "transcription":
            path = spec.get("endpoint_path", "")
            if (
                not path.startswith("/")
                or path.startswith("//")
                or "?" in path
                or "#" in path
                or ".." in path
            ):
                raise ValueError("音频chat endpoint必须是KIE下的相对路径")
        if role == "video":
            if spec["protocol"] == "kie_wan_reference_v1":
                if options.get("prompt_extend", False) is not False:
                    raise ValueError("Wan 3.0不支持prompt_extend；请关闭提示词扩展")
                if options.get("audio", False) is not False:
                    raise ValueError("Wan 3.0局部补底不生成音轨；原声由本地合成保留")
            if (
                options.get("resolution") not in {"720p", "1080p"}
                or options.get("duration", 0) != 0
                or options.get("audio_setting", "origin") != "origin"
            ):
                raise ValueError("视频编辑须保持全片与原声；resolution仅720p/1080p")
            prices = spec.get("estimated_credits_per_second", {})
            price = prices.get(options["resolution"])
            if not isinstance(price, (int, float)) or isinstance(price, bool) or price <= 0:
                raise ValueError("视频模型必须配置当前分辨率的正数预估单价")
        result[role] = {**spec, "catalog_id": key, "options": options}
    text = json.dumps(result, sort_keys=True, ensure_ascii=False)
    return {
        "version": catalog["version"],
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "models": result,
    }


def apply_selection(config, snapshot):
    """Use only known fields; credentials remain in the runtime config object."""
    from dataclasses import replace

    models = snapshot["models"]
    return replace(
        config,
        chat_model=models["transcription"]["model"],
        chat_path=models["transcription"]["endpoint_path"],
        tts_model=models["tts"]["model"],
        tts_protocol=("gemini38_speakers" if models["tts"]["protocol"] == "kie_gemini38_tts_v1"
                      else "gemini_speakers"),
        voice=models["tts"]["options"].get("voice", "Kore"),
        tts_options={
            **{k: v for k, v in models["tts"]["options"].items() if k != "voice"},
            "voice_name": models["tts"]["options"].get("voice", "Kore"),
        },
        video_model=models["video"]["model"],
        video_protocol=("wan_reference_video" if models["video"]["protocol"] == "kie_wan_reference_v1"
                        else "wan_video_edit"),
        video_options={k: v for k, v in models["video"]["options"].items()
                       if k not in ({"duration", "prompt_extend", "audio_setting"}
                                    if models["video"]["protocol"] == "kie_wan_reference_v1"
                                    else {"duration"})},
    )
