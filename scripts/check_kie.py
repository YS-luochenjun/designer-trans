"""Read KIE account credit status without generation, upload, or printing credentials."""

import json
import math
import os
from pathlib import Path
import httpx
from video_translation.config import Settings


def main():
    settings = Settings.load(Path(__file__).resolve().parents[1])
    key = os.environ.get("VT_KIE_API_KEY", os.environ.get("KIE_API_KEY", ""))
    result = {
        "provider": "kie",
        "credential_configured": bool(key),
        "authenticated": None,
        "has_credit": None,
        "provider_model_called": False,
        "endpoint": "https://api.kie.ai/api/v1/chat/credit",
    }
    if key:
        try:
            response = httpx.get(
                result["endpoint"],
                headers={"Authorization": "Bearer " + key},
                timeout=20,
                follow_redirects=False,
            )
            data = response.json()
            amount = data.get("data")
            if (
                response.status_code == 200
                and data.get("code") == 200
                and isinstance(amount, (int, float))
                and not isinstance(amount, bool)
                and math.isfinite(amount)
            ):
                result.update(authenticated=True, has_credit=amount > 0)
            elif response.status_code == 401 or data.get("code") == 401:
                result.update(authenticated=False, reason="unauthorized")
            else:
                result["reason"] = "temporarily_unverified"
        except Exception:
            result["reason"] = "temporarily_unverified"
    target = settings.runtime / "logs/kie-credential-check.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2), "utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
