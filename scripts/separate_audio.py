"""CPU UVR worker and explicit local command; no old configuration imports."""
from __future__ import annotations
import argparse
import json
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def worker(source, model, output, threads):
    import numpy as np
    import sherpa_onnx
    from video_translation.integrations.providers.separation import (
        ENGINE_VERSION, MODEL_SHA256, NUMPY_VERSION, SeparationError, sha256, wav_info,
    )
    if sherpa_onnx.__version__ != ENGINE_VERSION or np.__version__ != NUMPY_VERSION:
        raise SeparationError("Unsupported separation dependency version")
    if sha256(model) != MODEL_SHA256 or not 1 <= threads <= 8:
        raise SeparationError("Unregistered separation model or thread count")
    info = wav_info(source)
    with wave.open(str(source), "rb") as stream:
        samples = np.frombuffer(stream.readframes(stream.getnframes()), dtype="<i2")
        samples = samples.reshape(-1, 2).T.astype(np.float32) / 32768
    config = sherpa_onnx.OfflineSourceSeparationConfig(
        model=sherpa_onnx.OfflineSourceSeparationModelConfig(
            uvr=sherpa_onnx.OfflineSourceSeparationUvrModelConfig(model=str(model)),
            num_threads=threads, debug=False, provider="cpu"))
    if not config.validate():
        raise SeparationError("UVR model configuration invalid")
    separator = sherpa_onnx.OfflineSourceSeparation(config)
    result = separator.process(sample_rate=44100, samples=np.ascontiguousarray(samples))
    if len(result.stems) != 2 or result.sample_rate != 44100:
        raise SeparationError("Unexpected UVR output layout")
    output.mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(("background", "vocals")):
        # Pinned Inst_HQ_3 predicts instrumental first, unlike vocal UVR models.
        # Both stems come from Sherpa, without application-level subtraction.
        data = np.asarray(result.stems[index].data, dtype=np.float32).T
        if data.ndim != 2 or data.shape[1] != 2 or not np.isfinite(data).all():
            raise SeparationError("Nonfinite or malformed separated samples")
        if abs(len(data) / 44100 - info["duration_seconds"]) > .25:
            raise SeparationError("Separated duration mismatch")
        pcm = (np.clip(data, -1, 1) * 32767).astype("<i2")
        with wave.open(str(output / f"{name}.wav"), "wb") as stream:
            stream.setnchannels(2)
            stream.setsampwidth(2)
            stream.setframerate(44100)
            stream.writeframes(pcm.tobytes())
    return {"finite": True, "engine_version": sherpa_onnx.__version__, "sample_rate": 44100,
            "primary_stem": "background", "quality_state": "needs_review"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-status", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.worker_status:
        import numpy as np
        import sherpa_onnx
        result = {"engine_version": sherpa_onnx.__version__, "numpy_version": np.__version__}
    elif args.worker:
        if not args.source or not args.model or not args.output:
            parser.error("worker needs source, model and output")
        result = worker(args.source, args.model, args.output, args.threads)
    else:
        from video_translation.integrations.providers.separation import SeparationConfig, Separator
        config = SeparationConfig.from_env(ROOT)
        if args.status:
            result = config.status()
        else:
            if not args.source or not args.output:
                parser.error("provide --source (44.1 kHz stereo PCM WAV) and --output")
            result = Separator(config).separate(args.source, args.output)
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("Local separation failed: " + type(exc).__name__, file=sys.stderr)
        sys.exit(1)
