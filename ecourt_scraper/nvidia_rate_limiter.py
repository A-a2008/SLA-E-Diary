import threading
import time

_lock = threading.Lock()
_last_call: float = 0
_MIN_INTERVAL = 2.0  # 30 RPM = 1 request per 2 seconds


def wait():
    """Enforce 30 RPM across ALL NVIDIA API calls (OCR + LLM)."""
    global _last_call
    with _lock:
        now = time.monotonic()
        elapsed = now - _last_call
        if elapsed < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - elapsed)
        _last_call = time.monotonic()
