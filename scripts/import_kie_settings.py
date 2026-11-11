"""Import explicitly selected legacy KIE credentials locally; never calls a provider."""

from pathlib import Path
import argparse
import json
import os
import re


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-project", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source = args.source_project / ".env"
    values = {}
    for line in source.read_text("utf-8-sig").splitlines():
        match = re.fullmatch(
            r"\s*(?:export\s+)?(KIE_API_KEY|KIE_BASE_URL|KIE_UPLOAD_URL)\s*=\s*(.*?)\s*", line
        )
        if match:
            value = match[2]
            if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[match[1]] = value
    key = values.get("KIE_API_KEY", "")
    if not key or len(key) > 16384:
        raise SystemExit("Selected legacy project has no usable KIE_API_KEY; no secret was printed.")
    # Audited official hosts; never forward the old secret to an arbitrary value in a source file.
    api_url = "https://api.kie.ai"
    upload_url = "https://kieai.redpandaai.co/api/file-stream-upload"
    if (
        values.get("KIE_BASE_URL", api_url).rstrip("/") != api_url
        or values.get("KIE_UPLOAD_URL", upload_url) != upload_url
    ):
        raise SystemExit(
            "Legacy host differs from audited KIE hosts; review it before importing credentials."
        )
    target = root / "runtime/config/kie-private.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise SystemExit("Private KIE settings already exist; refusing to overwrite.")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"api_key": key, "api_url": api_url, "upload_url": upload_url}, handle, indent=2)
    print(
        json.dumps(
            {
                "imported": True,
                "key_present": True,
                "private_file": "runtime/config/kie-private.json",
                "api_url": api_url,
                "upload_url": upload_url,
                "provider_called": False,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
