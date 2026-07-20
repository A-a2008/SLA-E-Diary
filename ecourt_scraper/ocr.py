import base64
import json
import os
import requests

from .config import NVIDIA_OCR_URL, CAPTCHA_DEBUG_DIR
from .nvidia_rate_limiter import wait as nvidia_wait

NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")


def solve_captcha(image_bytes: bytes, debug_name: str = "captcha") -> tuple[str, dict]:
    """Send captcha image to NVIDIA Nemotron-OCR and return (parsed_text, raw_response)."""
    os.makedirs(CAPTCHA_DEBUG_DIR, exist_ok=True)

    debug_path = os.path.join(CAPTCHA_DEBUG_DIR, f"{debug_name}.png")
    with open(debug_path, "wb") as f:
        f.write(image_bytes)

    image_b64 = base64.b64encode(image_bytes).decode()

    assert len(image_b64) < 180_000, \
        "Image too large for inline payload; use assets API."

    headers = {
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
        "Accept": "application/json",
    }

    payload = {
        "input": [
            {
                "type": "image_url",
                "url": f"data:image/png;base64,{image_b64}",
            }
        ]
    }

    nvidia_wait()
    resp = requests.post(NVIDIA_OCR_URL, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    raw = resp.json()

    with open(os.path.join(CAPTCHA_DEBUG_DIR, f"{debug_name}_ocr.json"), "w") as f:
        json.dump(raw, f, indent=2)

    text = _extract_text(raw)
    return text.strip(), raw


def _extract_text(raw: dict) -> str:
    try:
        parts = []
        for det in raw["data"][0]["text_detections"]:
            t = det["text_prediction"]["text"]
            if t and t not in ("-", "—", ""):
                parts.append(t)
        if parts:
            best = max(parts, key=len)
            return best
    except (KeyError, TypeError, IndexError):
        pass

    try:
        return raw["choices"][0]["message"]["content"]
    except (KeyError, TypeError, IndexError):
        pass

    try:
        return raw["text"]
    except KeyError:
        pass

    try:
        return raw["result"]
    except KeyError:
        pass

    return ""
