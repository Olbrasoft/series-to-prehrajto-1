"""Parse both standard Retry-After formats without logging request URLs."""
import math
import time
from email.utils import parsedate_to_datetime


def retry_after_seconds(value, *, now=None, default=300):
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            delay = parsedate_to_datetime(value).timestamp() - (time.time() if now is None else now)
        except (TypeError, ValueError, OverflowError):
            return default
    return max(0, math.ceil(delay)) if math.isfinite(delay) else default
