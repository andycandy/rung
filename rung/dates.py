"""Dates. The model resolves what the requester wrote ("Monday", "a week from Friday")
relative to a `today` it is given; code only checks the result is a real ISO date."""
from __future__ import annotations

import os
import time
from datetime import date

os.environ.setdefault("TZ", "Asia/Kolkata")
if os.environ.get("TZ") == "Asia/Calcutta":  # tzlocal rejects the legacy alias
    os.environ["TZ"] = "Asia/Kolkata"
if hasattr(time, "tzset"):
    time.tzset()


def is_iso_date(s) -> bool:
    """True for a calendar date written exactly YYYY-MM-DD."""
    if not isinstance(s, str) or len(s) != 10 or s[4] != "-" or s[7] != "-":
        return False
    try:
        date.fromisoformat(s)
    except ValueError:
        return False
    return True
