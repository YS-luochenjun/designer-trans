"""Verified quote for the image protocol/resolution actually implemented."""


def image_quote(snapshot):
    if not snapshot or (
        snapshot.get("provider") != "kie"
        or snapshot.get("protocol") != "kie_image_to_image"
        or snapshot.get("model") != "gpt-image-2-image-to-image"
        or snapshot.get("resolution") != "1K"
    ):
        return None
    return {
        "provider": "kie", "model": snapshot["model"], "resolution": "1K",
        "estimated_credits": 6, "basis": "one output image",
        "verified_on": "2026-11-09", "source": "https://kie.ai/gpt-image-2",
    }
